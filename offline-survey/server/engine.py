"""业务引擎：修订关系、指纹核验、字段级自动合并、冲突留痕与发布门禁。

核心时序（每个回传都严格按这两步走，二者解耦）：
1. 接收确认：原始报文先落盘/入库，立刻开具 receipt。收据只代表"原始回传可靠保存"，
   不代表合并成功、更不代表发布。
2. 入队处理：操作按 seq FIFO 处理。旧合并任务若发现答卷已被更新的撤回终结，
   直接 blocked，绝不越过撤回去覆盖。
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .store import Store, now_iso, short_id

# ----------------------------- 指纹 -----------------------------
# 指纹 = 对整卷字段当前值做规范化 JSON（键排序、无空白）后的 SHA-256。
# 设备端 JS 用完全相同的算法（见 static/js/common.js），
# 因此指纹不符意味着设备基准被本地篡改/损坏，而非正常并发编辑。


def canonical_fields(fields: dict[str, Any]) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint_of(fields: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_fields(fields).encode("utf-8")).hexdigest()


# ----------------------------- 引擎 -----------------------------
class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ===================== 演示数据初始化 =====================
    def seed(self) -> dict:
        s = self.store
        existing = s.query_one("SELECT id FROM response LIMIT 1")
        if existing:
            return {"reset": False}

        survey = {
            "fields": [
                {"key": "customer", "label": "客户名称", "type": "text"},
                {"key": "contact", "label": "联系人电话", "type": "text"},
                {"key": "satisfaction", "label": "满意度评分(1-5)", "type": "number"},
                {"key": "comment", "label": "现场备注", "type": "textarea"},
            ],
            "attachments": [
                {"slot": "id_photo", "label": "现场照片", "required": True},
                {"slot": "signature", "label": "客户签名", "required": True},
            ],
        }
        survey_id = "SRV-001"
        response_id = "RSP-DEMO-01"
        s.execute(
            "INSERT INTO survey(id,title,version,definition) VALUES(?,?,?,?)",
            (survey_id, "客户回访演示问卷", 1, json.dumps(survey, ensure_ascii=False)),
        )
        now = now_iso()
        s.execute(
            "INSERT INTO response(id,survey_id,status,head_rev,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (response_id, survey_id, "editing", 0, now, now),
        )
        # rev=0 空白答卷：所有字段为空串，两台设备的共同祖先
        fields = {f["key"]: "" for f in survey["fields"]}
        for key, value in fields.items():
            s.execute(
                "INSERT INTO response_field(response_id,field_key,value,rev,origin_device)"
                " VALUES(?,?,?,?,?)",
                (response_id, key, json.dumps(value, ensure_ascii=False), 0, "seed"),
            )
        fp = fingerprint_of(fields)
        s.execute(
            "INSERT INTO rev_fingerprint(response_id,rev,fingerprint,fields_json) VALUES(?,?,?,?)",
            (response_id, 0, fp, canonical_fields(fields)),
        )
        for key in fields:
            s.execute(
                "INSERT INTO field_revision(response_id,field_key,rev,value,origin_device,"
                "reason,changed_at) VALUES(?,?,?,?,?,?,?)",
                (response_id, key, 0, json.dumps("", ensure_ascii=False), "seed", "initial", now),
            )
        s.audit(response_id, "seed", "初始化演示问卷与空白答卷 rev=0")
        s.commit()
        return {"reset": True, "response_id": response_id}

    def reset(self) -> dict:
        s = self.store
        for table in [
            "audit_log", "idempotency", "conflict", "branch", "op_queue",
            "raw_submission", "attachment", "device_cursor", "field_revision",
            "rev_fingerprint", "response_field", "response", "survey",
        ]:
            s.execute(f"DELETE FROM {table}")
        s.commit()
        s.audit(None, "reset", "演示环境已重置")
        s.commit()
        return self.seed()

    # ===================== 接收确认（第一步：存证+收据） =====================
    def receive(self, envelope: dict) -> dict:
        """接收任意回传报文。无论后续合并结果如何，先可靠保存原文并开收据。"""
        s = self.store
        with s.lock:
            receipt_id = short_id("RCP")
            now = now_iso()
            device_id = envelope.get("device_id")
            response_id = envelope.get("response_id")
            kind = envelope.get("kind", "unknown")
            client_sub_id = envelope.get("submission_id", "?")

            # 非 JSON 结构无法处理：存证但不入队，收据上如实标注
            if not isinstance(envelope, dict) or not response_id or not kind:
                s.execute(
                    "INSERT INTO raw_submission(receipt_id,client_submission_id,device_id,"
                    "response_id,kind,payload,received_at,integrity_status,duplicate_of,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, str(client_sub_id), device_id, response_id, kind,
                     json.dumps(envelope, ensure_ascii=False), now, "invalid_json",
                     None, "报文缺少 response_id/kind，无法入队"),
                )
                s.audit(response_id, "receive.invalid", f"收据 {receipt_id} 报文结构不合法", receipt_id)
                s.commit()
                return {"receipt_id": receipt_id, "received_at": now,
                        "integrity_status": "invalid_json",
                        "queued": False,
                        "note": "原始报文已可靠保存，但因结构不合法未进入处理队列"}

            # 重复回传（同一客户端提交号）：仍开新收据，但标记 duplicate，不再入队
            dup = s.query_one(
                "SELECT receipt_id FROM raw_submission WHERE client_submission_id=?",
                (client_sub_id,),
            )
            if dup:
                s.execute(
                    "INSERT INTO raw_submission(receipt_id,client_submission_id,device_id,"
                    "response_id,kind,payload,received_at,integrity_status,duplicate_of,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, client_sub_id, device_id, response_id, kind,
                     json.dumps(envelope, ensure_ascii=False), now, "duplicate",
                     dup["receipt_id"], "重复回传，已幂等忽略，仅存证"),
                )
                s.audit(response_id, "receive.duplicate",
                        f"收据 {receipt_id} 是 {dup['receipt_id']} 的重复回传", receipt_id)
                s.commit()
                return {"receipt_id": receipt_id, "received_at": now,
                        "integrity_status": "duplicate",
                        "duplicate_of": dup["receipt_id"], "queued": False,
                        "note": "原始报文已可靠保存；检测为重复回传，未重复处理"}

            integrity = "valid"
            note = None
            # 答卷类回传先做指纹核验：不符只影响处理方式，不影响存证与收据
            if kind == "answers":
                integrity, note = self._check_fingerprint(envelope)

            s.execute(
                "INSERT INTO raw_submission(receipt_id,client_submission_id,device_id,"
                "response_id,kind,payload,received_at,integrity_status,duplicate_of,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (receipt_id, client_sub_id, device_id, response_id, kind,
                 json.dumps(envelope, ensure_ascii=False), now, integrity, None, note),
            )
            cur = s.execute(
                "INSERT INTO op_queue(receipt_id,response_id,device_id,op_type,state,"
                "client_op_id,payload) VALUES(?,?,?,?, 'pending',?,?)",
                (receipt_id, response_id, device_id, kind,
                 envelope.get("op_id"), json.dumps(envelope, ensure_ascii=False)),
            )
            seq = cur.lastrowid
            s.audit(response_id, f"receive.{kind}",
                    f"收据 {receipt_id}（seq={seq}）已存并入队，完整性={integrity}", receipt_id, seq)
            s.commit()

            # 第二步立即在队列上处理（队列仍是 FIFO 依据，重启后 pending 可重放）
            result = self._process_next()
            return {"receipt_id": receipt_id, "received_at": now, "seq": seq,
                    "integrity_status": integrity, "queued": True,
                    "fingerprint_note": note,
                    "process_result": result}

    def _check_fingerprint(self, envelope: dict) -> tuple[str, str | None]:
        s = self.store
        response_id = envelope["response_id"]
        base_rev = int(envelope.get("base_rev", -1))
        claimed = envelope.get("base_fingerprint")
        row = s.query_one(
            "SELECT fingerprint FROM rev_fingerprint WHERE response_id=? AND rev=?",
            (response_id, base_rev),
        )
        if row is None:
            return "fingerprint_mismatch", f"基准修订 rev={base_rev} 在服务端不存在"
        if claimed != row["fingerprint"]:
            return ("fingerprint_mismatch",
                    f"指纹不符：设备声称基准 rev={base_rev} 的指纹为 {str(claimed)[:12]}…，"
                    f"服务端记录为 {row['fingerprint'][:12]}…，本地基准可能被篡改")
        return "valid", None

    # ===================== 队列处理（第二步） =====================
    def _process_next(self) -> dict:
        s = self.store
        row = s.query_one(
            "SELECT * FROM op_queue WHERE state='pending' ORDER BY seq LIMIT 1"
        )
        if row is None:
            return {"processed": False, "reason": "队列为空"}
        envelope = json.loads(row["payload"])
        seq = row["seq"]
        try:
            if row["op_type"] == "answers":
                result = self._handle_answers(envelope, row)
            elif row["op_type"] == "withdraw":
                result = self._handle_withdraw(envelope, row)
            elif row["op_type"] == "attach":
                result = self._handle_attach(envelope, row)
            elif row["op_type"] == "delete_attach":
                result = self._handle_delete_attach(envelope, row)
            else:
                result = {"state": "blocked", "reason": f"未知操作类型 {row['op_type']}"}
        except Exception as exc:  # 防御：单条异常不拖垮队列
            result = {"state": "blocked", "reason": f"处理异常: {exc}"}
            s.audit(row["response_id"], "op.error", str(exc), row["receipt_id"], seq)

        s.execute(
            "UPDATE op_queue SET state=?, processed_at=?, result=? WHERE seq=?",
            (result.get("state", "blocked"), now_iso(),
             json.dumps(result, ensure_ascii=False), seq),
        )
        self._recompute_status(row["response_id"])
        s.commit()
        return {"processed": True, "seq": seq, **result}

    def _idempotency_check(self, response_id: str, device_id: str,
                           client_op_id: str | None, seq: int) -> dict | None:
        """返回已处理结果表示幂等命中；None 表示首次。"""
        if not client_op_id:
            return None
        s = self.store
        row = s.query_one(
            "SELECT result FROM idempotency WHERE response_id=? AND device_id=? AND client_op_id=?",
            (response_id, device_id or "", client_op_id),
        )
        if row:
            return {"state": "idempotent",
                    "reason": f"客户端操作号 {client_op_id} 已处理过，本次不重复生效",
                    "previous": json.loads(row["result"])}
        return None

    def _save_idempotency(self, response_id: str, device_id: str,
                          client_op_id: str | None, result: dict) -> None:
        if not client_op_id:
            return
        self.store.execute(
            "INSERT OR REPLACE INTO idempotency(response_id,device_id,client_op_id,result,at)"
            " VALUES(?,?,?,?,?)",
            (response_id, device_id or "", client_op_id,
             json.dumps(result, ensure_ascii=False), now_iso()),
        )

    # ---------- 答卷：3-way 字段级自动合并 ----------
    def _handle_answers(self, envelope: dict, qrow) -> dict:
        s = self.store
        response_id = envelope["response_id"]
        device_id = envelope.get("device_id")
        base_rev = int(envelope.get("base_rev", 0))
        client_op_id = envelope.get("op_id")
        seq = qrow["seq"]
        submitted = envelope.get("fields", {})
        now = now_iso()

        idem = self._idempotency_check(response_id, device_id, client_op_id, seq)
        if idem:
            s.audit(response_id, "op.idempotent", idem["reason"], qrow["receipt_id"], seq)
            return idem

        resp = s.query_one("SELECT * FROM response WHERE id=?", (response_id,))
        if resp is None:
            return {"state": "blocked", "reason": f"答卷 {response_id} 不存在"}

        # 旧合并任务不能越过新撤回：已撤回答卷上的编辑一律阻断
        if resp["status"] == "withdrawn":
            reason = ("答卷已被更新的撤回操作终结，该编辑任务排在撤回之后，"
                      "按 FIFO 规则阻断，旧修改不得覆盖撤回状态")
            s.audit(response_id, "op.blocked", reason, qrow["receipt_id"], seq)
            return {"state": "blocked", "reason": reason}

        integrity = qrow and self._integrity_of(qrow["receipt_id"])

        # 指纹不符：不自动合并，保留整份分支，登记人工冲突
        if integrity == "fingerprint_mismatch":
            note = self._note_of(qrow["receipt_id"])
            conflict_id = short_id("CFL")
            branch_id = self._save_branch(response_id, envelope, qrow, device_id, now)
            detail = {
                "receipt_id": qrow["receipt_id"], "device_id": device_id,
                "base_rev": base_rev, "submitted_fields": submitted,
                "branch_id": branch_id, "note": note,
            }
            s.execute(
                "INSERT INTO conflict(id,response_id,conflict_type,status,field_key,"
                "attachment_id,basis,detail,created_at) VALUES(?,?,?, 'open',NULL,NULL,?,?,?)",
                (conflict_id, response_id, "fingerprint",
                 f"指纹核验未通过，系统不敢自动合并，已原样保留整份分支 {branch_id} 等待人工裁决",
                 json.dumps(detail, ensure_ascii=False), now),
            )
            s.audit(response_id, "conflict.fingerprint",
                    f"{conflict_id}：{note}；分支 {branch_id} 已保留", qrow["receipt_id"], seq)
            result = {"state": "conflicted", "conflict_id": conflict_id,
                      "branch_id": branch_id, "reason": note}
            self._save_idempotency(response_id, device_id, client_op_id, result)
            return result

        # 取 3-way 的 base。设备自报的 base_rev 可能落后（离线期间别人已提交），
        # 但其基准指纹通过了核验，因此按指纹反查设备真实基准修订：
        # 设备报 rev1 而头部已到 rev3 时，base 应取 rev1，head 取 rev3，分叉才能被识别。
        claimed_fp = envelope.get("base_fingerprint")
        base_row = s.query_one(
            "SELECT rev,fields_json FROM rev_fingerprint"
            " WHERE response_id=? AND fingerprint=? ORDER BY rev LIMIT 1",
            (response_id, claimed_fp),
        )
        if base_row is None:
            base_row = s.query_one(
                "SELECT rev,fields_json FROM rev_fingerprint WHERE response_id=? AND rev=?",
                (response_id, base_rev),
            )
        if base_row is None:
            return {"state": "blocked", "reason": f"基准修订 rev={base_rev} 不存在，无法合并"}
        base_fields = json.loads(base_row["fields_json"])

        head_rev = int(resp["head_rev"])
        head_fields = {
            r["field_key"]: json.loads(r["value"])
            for r in s.query(
                "SELECT field_key,value FROM response_field WHERE response_id=?",
                (response_id,),
            )
        }

        auto_applied: list[dict] = []
        conflicts: list[dict] = []
        merged: dict[str, Any] = dict(head_fields)
        survey = self.get_survey(response_id)
        valid_keys = {f["key"] for f in survey["fields"]}

        # 协议：设备只回传"发生变化的字段"（delta）。未提及的字段在三方合并中
        # 视为设备沿用 base 值，绝不能用空值覆盖服务端（旧答卷不能盖新修改）。
        for key in valid_keys:
            if key not in submitted:
                continue
            new_value = submitted[key]
            base_value = base_fields.get(key, "")
            head_value = head_fields.get(key, "")
            # 规则 1：设备回传值与 base 一致（显式清空到 base 值也视为无变更）→ 不动
            if new_value == base_value:
                continue
            # 规则 2：服务端头部仍是 base（别人没动过）→ 直接自动应用
            if head_value == base_value:
                merged[key] = new_value
                auto_applied.append({"field_key": key, "rule": "fast-forward",
                                     "old": head_value, "new": new_value})
                continue
            # 规则 3：两边都改了，但改成同一个值 → 收敛，同样落修订
            if new_value == head_value:
                merged[key] = new_value
                auto_applied.append({"field_key": key, "rule": "converged",
                                     "value": new_value})
                continue
            # 规则 4：两边改成不同值 → 字段冲突，保留双方值，留人工裁决
            conflict_id = short_id("CFL")
            detail = {
                "receipt_id": qrow["receipt_id"], "device_id": device_id,
                "base_rev": base_rev, "head_rev": head_rev,
                "base": base_value, "head": head_value, "incoming": new_value,
            }
            basis = (f"字段 {key} 三方分叉：基准值={base_value!r}，"
                     f"服务端当前值={head_value!r}（其他设备已提交），"
                     f"本次回传值={new_value!r}（设备 {device_id}），系统无法自动取舍")
            s.execute(
                "INSERT INTO conflict(id,response_id,conflict_type,status,field_key,"
                "attachment_id,basis,detail,created_at) VALUES(?,?,?, 'open',?,NULL,?,?,?)",
                (conflict_id, response_id, "field", key, basis,
                 json.dumps(detail, ensure_ascii=False), now),
            )
            conflicts.append({"conflict_id": conflict_id, "field_key": key,
                              "base": base_value, "head": head_value, "incoming": new_value})

        # 自动合并的字段写新修订；冲突字段保持服务端头部值（旧答卷不能盖新修改）
        new_rev = head_rev
        if auto_applied:
            new_rev = head_rev + 1
            for item in auto_applied:
                key = item["field_key"]
                val = merged[key]
                s.execute(
                    "UPDATE response_field SET value=?, rev=?, origin_device=?"
                    " WHERE response_id=? AND field_key=?",
                    (json.dumps(val, ensure_ascii=False), new_rev, device_id,
                     response_id, key),
                )
                s.execute(
                    "INSERT INTO field_revision(response_id,field_key,rev,value,origin_device,"
                    "reason,changed_at) VALUES(?,?,?,?,?,?,?)",
                    (response_id, key, new_rev, json.dumps(val, ensure_ascii=False),
                     device_id, f"auto-merge:{item['rule']}", now),
                )
                s.audit(response_id, "field.auto",
                        f"字段 {key} 自动合并（{item['rule']}）→ {val!r}",
                        qrow["receipt_id"], seq)
            fp = fingerprint_of(merged)
            s.execute(
                "INSERT INTO rev_fingerprint(response_id,rev,fingerprint,fields_json)"
                " VALUES(?,?,?,?)",
                (response_id, new_rev, fp, canonical_fields(merged)),
            )
            s.execute("UPDATE response SET head_rev=?, updated_at=? WHERE id=?",
                      (new_rev, now, response_id))

        # 推进该设备的同步位点到新头部（含新指纹），其基准被篡改的情况不会走到这里
        s.execute(
            "INSERT INTO device_cursor(response_id,device_id,base_rev,base_fingerprint,updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(response_id,device_id) DO UPDATE SET"
            " base_rev=excluded.base_rev, base_fingerprint=excluded.base_fingerprint,"
            " updated_at=excluded.updated_at",
            (response_id, device_id or "", new_rev,
             fingerprint_of(merged), now),
        )

        if conflicts:
            s.audit(response_id, "op.conflicted",
                    f"{len(auto_applied)} 个字段自动合并，{len(conflicts)} 个字段冲突待裁决",
                    qrow["receipt_id"], seq)
            result = {"state": "conflicted", "auto_applied": auto_applied,
                      "conflicts": conflicts, "new_head_rev": new_rev}
        else:
            s.audit(response_id, "op.applied",
                    f"全部自动合并完成，head_rev={new_rev}", qrow["receipt_id"], seq)
            result = {"state": "applied", "auto_applied": auto_applied,
                      "new_head_rev": new_rev}
        self._save_idempotency(response_id, device_id, client_op_id, result)
        return result

    # ---------- 撤回 ----------
    def _handle_withdraw(self, envelope: dict, qrow) -> dict:
        s = self.store
        response_id = envelope["response_id"]
        device_id = envelope.get("device_id")
        client_op_id = envelope.get("op_id")
        seq = qrow["seq"]
        reason_text = envelope.get("reason", "设备发起撤回")

        idem = self._idempotency_check(response_id, device_id, client_op_id, seq)
        if idem:
            return idem

        resp = s.query_one("SELECT * FROM response WHERE id=?", (response_id,))
        if resp is None:
            return {"state": "blocked", "reason": f"答卷 {response_id} 不存在"}

        if resp["status"] == "withdrawn":
            result = {"state": "idempotent", "reason": "答卷已是撤回状态，重复撤回不重复生效"}
            s.audit(response_id, "withdraw.idempotent", result["reason"], qrow["receipt_id"], seq)
            return result

        s.execute("UPDATE response SET status='withdrawn', updated_at=? WHERE id=?",
                  (now_iso(), response_id))
        # 撤回参与冲突判断：登记一条撤回事件，页面可见；附件保持原状但不再可发布
        conflict_id = short_id("CFL")
        detail = {"receipt_id": qrow["receipt_id"], "device_id": device_id,
                  "reason": reason_text, "at": now_iso()}
        s.execute(
            "INSERT INTO conflict(id,response_id,conflict_type,status,field_key,"
            "attachment_id,basis,detail,created_at) VALUES(?,?,?, 'open',NULL,NULL,?,?,?)",
            (conflict_id, response_id, "withdrawal",
             f"答卷被设备 {device_id} 撤回（原因：{reason_text}）。"
             "撤回之后到达的编辑/附件任务按 FIFO 阻断；需人工决定恢复或终结。",
             json.dumps(detail, ensure_ascii=False), now_iso()),
        )
        s.audit(response_id, "withdraw",
                f"答卷撤回，{conflict_id} 登记撤回事件；其后排队任务将被拦截",
                qrow["receipt_id"], seq)
        result = {"state": "applied", "withdrawn": True, "conflict_id": conflict_id}
        self._save_idempotency(response_id, device_id, client_op_id, result)
        return result

    # ---------- 附件：同名挂错防护 + 版本冲突 ----------
    def _handle_attach(self, envelope: dict, qrow) -> dict:
        s = self.store
        response_id = envelope["response_id"]
        device_id = envelope.get("device_id")
        client_op_id = envelope.get("op_id")
        seq = qrow["seq"]
        att = envelope.get("attachment", {})
        now = now_iso()

        idem = self._idempotency_check(response_id, device_id, client_op_id, seq)
        if idem:
            return idem

        resp = s.query_one("SELECT status FROM response WHERE id=?", (response_id,))
        if resp is None:
            return {"state": "blocked", "reason": f"答卷 {response_id} 不存在"}
        if resp["status"] == "withdrawn":
            reason = "答卷已撤回，附件上传任务排在撤回之后，按 FIFO 阻断"
            s.audit(response_id, "op.blocked", reason, qrow["receipt_id"], seq)
            return {"state": "blocked", "reason": reason}

        attachment_id = att.get("attachment_id")
        slot = att.get("slot", "")
        file_name = att.get("file_name", "")
        digest, size, _path = s.write_blob(att.get("content_base64", ""))

        existing = s.query_one(
            "SELECT * FROM attachment WHERE response_id=? AND attachment_id=?",
            (response_id, attachment_id),
        )

        if existing:
            # 同一稳定 ID + 同一内容：幂等重传
            if existing["content_hash"] == digest and existing["status"] == "present":
                result = {"state": "idempotent",
                          "reason": f"附件 {attachment_id} 内容未变化（哈希 {digest[:12]}…），重传不重复生效"}
                s.audit(response_id, "attach.idempotent", result["reason"], qrow["receipt_id"], seq)
                return result
            # 同一稳定 ID 不同内容：版本冲突，不覆盖旧版本，留人工裁决
            conflict_id = short_id("CFL")
            staged_id = f"{attachment_id}__staged_{conflict_id[-6:]}"
            detail = {"receipt_id": qrow["receipt_id"], "attachment_id": attachment_id,
                      "staged_attachment_id": staged_id,
                      "old_hash": existing["content_hash"], "new_hash": digest,
                      "old_name": existing["file_name"], "new_name": file_name}
            basis = (f"附件 {attachment_id} 出现两个版本：已存 {existing['file_name']!r}"
                     f"（{existing['content_hash'][:12]}…，v{existing['version']}），"
                     f"本次回传 {file_name!r}（{digest[:12]}…）。系统保留双版本，不自动覆盖。")
            s.execute(
                "INSERT INTO conflict(id,response_id,conflict_type,status,field_key,"
                "attachment_id,basis,detail,created_at) VALUES(?,?,?, 'open',NULL,?,?,?,?)",
                (conflict_id, response_id, "attachment_version", attachment_id, basis,
                 json.dumps(detail, ensure_ascii=False), now),
            )
            # 新版本先以独立行暂存（ID 加后缀），人工裁决时再决定取哪个
            staged_id = f"{attachment_id}__staged_{conflict_id[-6:]}"
            s.execute(
                "INSERT INTO attachment(response_id,attachment_id,slot,file_name,content_hash,"
                "size_bytes,version,status,origin_device,updated_at)"
                " VALUES(?,?,?,?,?,?,?, 'present',?,?)",
                (response_id, staged_id, slot, file_name, digest, size,
                 existing["version"] + 1, device_id, now),
            )
            s.audit(response_id, "conflict.attachment_version",
                    f"{conflict_id}：{basis}", qrow["receipt_id"], seq)
            result = {"state": "conflicted", "conflict_id": conflict_id,
                      "staged_attachment_id": staged_id}
            self._save_idempotency(response_id, device_id, client_op_id, result)
            return result

        # 新 ID：同名附件不能挂错——同一 slot 下若已有不同 ID 的同名/现存文件，登记冲突
        sibling = s.query_one(
            "SELECT * FROM attachment WHERE response_id=? AND slot=? AND status='present'"
            " AND attachment_id<>? AND file_name=?",
            (response_id, slot, attachment_id, file_name),
        )
        s.execute(
            "INSERT INTO attachment(response_id,attachment_id,slot,file_name,content_hash,"
            "size_bytes,version,status,origin_device,updated_at)"
            " VALUES(?,?,?,?,?,?, 1, 'present',?,?)",
            (response_id, attachment_id, slot, file_name, digest, size, device_id, now),
        )
        if sibling:
            conflict_id = short_id("CFL")
            detail = {"receipt_id": qrow["receipt_id"], "slot": slot,
                      "existing_id": sibling["attachment_id"],
                      "existing_name": sibling["file_name"],
                      "incoming_id": attachment_id, "incoming_name": file_name}
            basis = (f"槽位 {slot} 下出现同名附件：已挂 {sibling['attachment_id']}"
                     f"（{sibling['file_name']!r}），本次 {attachment_id}（{file_name!r}）。"
                     "两份均已暂存，需人工确认归属，防止同名挂错。")
            s.execute(
                "INSERT INTO conflict(id,response_id,conflict_type,status,field_key,"
                "attachment_id,basis,detail,created_at) VALUES(?,?,?, 'open',NULL,?,?,?,?)",
                (conflict_id, response_id, "attachment_name", attachment_id, basis,
                 json.dumps(detail, ensure_ascii=False), now),
            )
            s.audit(response_id, "conflict.attachment_name",
                    f"{conflict_id}：{basis}", qrow["receipt_id"], seq)
            result = {"state": "conflicted", "conflict_id": conflict_id,
                      "attachment_id": attachment_id, "content_hash": digest}
        else:
            s.audit(response_id, "attach.applied",
                    f"附件 {file_name} 已挂载到槽位 {slot}（{digest[:12]}…）",
                    qrow["receipt_id"], seq)
            result = {"state": "applied", "attachment_id": attachment_id,
                      "content_hash": digest, "size_bytes": size}
        self._save_idempotency(response_id, device_id, client_op_id, result)
        return result

    # ---------- 附件删除：删除也参与冲突判断 ----------
    def _handle_delete_attach(self, envelope: dict, qrow) -> dict:
        s = self.store
        response_id = envelope["response_id"]
        device_id = envelope.get("device_id")
        client_op_id = envelope.get("op_id")
        seq = qrow["seq"]
        attachment_id = envelope.get("attachment_id")
        now = now_iso()

        idem = self._idempotency_check(response_id, device_id, client_op_id, seq)
        if idem:
            return idem

        resp = s.query_one("SELECT status FROM response WHERE id=?", (response_id,))
        if resp is None:
            return {"state": "blocked", "reason": f"答卷 {response_id} 不存在"}
        if resp["status"] == "withdrawn":
            reason = "答卷已撤回，附件删除任务排在撤回之后，按 FIFO 阻断"
            s.audit(response_id, "op.blocked", reason, qrow["receipt_id"], seq)
            return {"state": "blocked", "reason": reason}

        row = s.query_one(
            "SELECT * FROM attachment WHERE response_id=? AND attachment_id=?",
            (response_id, attachment_id),
        )
        if row is None:
            return {"state": "idempotent", "reason": f"附件 {attachment_id} 不存在，删除视为幂等空操作"}
        if row["status"] == "deleted":
            return {"state": "idempotent", "reason": f"附件 {attachment_id} 已是删除状态"}

        # 删除 vs 更新冲突：该附件若存在未决的版本冲突，删除不能静默吞掉新版本
        open_version_conflict = s.query_one(
            "SELECT id FROM conflict WHERE response_id=? AND status='open'"
            " AND conflict_type='attachment_version' AND detail LIKE ?",
            (response_id, f'%"{attachment_id}"%'),
        )
        s.execute(
            "UPDATE attachment SET status='deleted', updated_at=? WHERE response_id=? AND attachment_id=?",
            (now, response_id, attachment_id),
        )
        if open_version_conflict:
            conflict_id = short_id("CFL")
            detail = {"receipt_id": qrow["receipt_id"], "attachment_id": attachment_id,
                      "related_conflict": open_version_conflict["id"]}
            basis = (f"设备 {device_id} 删除附件 {attachment_id}，但该附件存在未决版本冲突 "
                     f"{open_version_conflict['id']}。删除已登记为软删除（tombstone），"
                     "blob 保留可恢复，需人工确认删除的是哪个版本。")
            s.execute(
                "INSERT INTO conflict(id,response_id,conflict_type,status,field_key,"
                "attachment_id,basis,detail,created_at) VALUES(?,?,?, 'open',NULL,?,?,?,?)",
                (conflict_id, response_id, "attachment_delete", attachment_id, basis,
                 json.dumps(detail, ensure_ascii=False), now),
            )
            s.audit(response_id, "conflict.attachment_delete", basis, qrow["receipt_id"], seq)
            result = {"state": "conflicted", "conflict_id": conflict_id}
        else:
            s.audit(response_id, "attach.deleted",
                    f"附件 {attachment_id} 软删除（tombstone，blob 保留）", qrow["receipt_id"], seq)
            result = {"state": "applied", "deleted": attachment_id}
        self._save_idempotency(response_id, device_id, client_op_id, result)
        return result

    # ===================== 分支保留 =====================
    def _save_branch(self, response_id: str, envelope: dict, qrow,
                     device_id: str | None, now: str) -> str:
        s = self.store
        branch_id = short_id("BRN")
        s.execute(
            "INSERT INTO branch(id,response_id,name,fields_json,source_receipt,device_id,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (branch_id, response_id,
             f"设备 {device_id} 的整份回传（基于 rev {envelope.get('base_rev')}）",
             canonical_fields(envelope.get("fields", {})),
             qrow["receipt_id"], device_id, now),
        )
        return branch_id

    # ===================== 人工裁决 =====================
    def resolve_conflict(self, conflict_id: str, resolution: str,
                         chosen_value: Any = None, chosen_attachment_id: str | None = None,
                         restore_response: bool = False) -> dict:
        s = self.store
        with s.lock:
            row = s.query_one("SELECT * FROM conflict WHERE id=?", (conflict_id,))
            if row is None:
                return {"ok": False, "reason": "冲突不存在"}
            if row["status"] == "resolved":
                return {"ok": False, "reason": "该冲突已裁决，不能重复处理"}
            now = now_iso()
            detail = json.loads(row["detail"])
            response_id = row["response_id"]
            result: dict[str, Any] = {"ok": True, "conflict_id": conflict_id}

            if row["conflict_type"] == "field":
                if chosen_value is None:
                    return {"ok": False, "reason": "字段冲突必须提供 chosen_value"}
                key = row["field_key"]
                head = s.query_one(
                    "SELECT rev FROM response_field WHERE response_id=? AND field_key=?",
                    (response_id, key),
                )
                new_rev = int(head["rev"]) + 1
                s.execute(
                    "UPDATE response_field SET value=?, rev=?, origin_device='manual'"
                    " WHERE response_id=? AND field_key=?",
                    (json.dumps(chosen_value, ensure_ascii=False), new_rev, response_id, key),
                )
                s.execute(
                    "INSERT INTO field_revision(response_id,field_key,rev,value,origin_device,"
                    "reason,changed_at) VALUES(?,?,?,?, 'manual',?,?)",
                    (response_id, key, new_rev,
                     json.dumps(chosen_value, ensure_ascii=False),
                     f"人工裁决:{resolution}", now),
                )
                fields = {r["field_key"]: json.loads(r["value"])
                          for r in s.query(
                              "SELECT field_key,value FROM response_field WHERE response_id=?",
                              (response_id,))}
                fp = fingerprint_of(fields)
                s.execute(
                    "INSERT INTO rev_fingerprint(response_id,rev,fingerprint,fields_json)"
                    " VALUES(?,?,?,?)",
                    (response_id, new_rev, fp, canonical_fields(fields)),
                )
                s.execute("UPDATE response SET head_rev=?, updated_at=? WHERE id=?",
                          (new_rev, now, response_id))
                result["new_rev"] = new_rev
                result["chosen_value"] = chosen_value

            elif row["conflict_type"] == "fingerprint":
                if resolution == "apply_branch":
                    branch_id = detail.get("branch_id")
                    result.update(self._apply_branch(branch_id, conflict_id, now))
                elif resolution == "discard_branch":
                    s.audit(response_id, "branch.discard",
                            f"分支 {detail.get('branch_id')} 经人工裁决丢弃", None, None)
                    result["discarded_branch"] = detail.get("branch_id")
                else:
                    return {"ok": False, "reason": "指纹冲突裁决必须是 apply_branch / discard_branch"}

            elif row["conflict_type"] == "attachment_version":
                if not chosen_attachment_id:
                    return {"ok": False, "reason": "附件版本冲突必须指定 chosen_attachment_id"}
                keep = s.query_one(
                    "SELECT * FROM attachment WHERE response_id=? AND attachment_id=?",
                    (response_id, chosen_attachment_id),
                )
                if keep is None:
                    return {"ok": False, "reason": "选定的附件不存在"}
                # 用选定版本归并回稳定 ID，其余暂存版本软删除。
                # 软删除不释放主键，因此先把原稳定 ID 行改名为退役 ID，
                # 再把选中的暂存行改名归位，最后统一把非选中行标记删除。
                base_id = detail.get("attachment_id") or row["attachment_id"]
                s.execute(
                    "UPDATE attachment SET attachment_id=?, updated_at=?"
                    " WHERE response_id=? AND attachment_id=?",
                    (f"{base_id}__retired_{conflict_id[-6:]}", now,
                     response_id, base_id),
                )
                s.execute(
                    "UPDATE attachment SET attachment_id=?, status='present', updated_at=?"
                    " WHERE response_id=? AND attachment_id=?",
                    (base_id, now, response_id, chosen_attachment_id),
                )
                for r in s.query(
                    "SELECT attachment_id FROM attachment WHERE response_id=?"
                    " AND (attachment_id LIKE ? OR attachment_id LIKE ?)",
                    (response_id, f"{base_id}__staged_%", f"{base_id}__retired_%"),
                ):
                    if r["attachment_id"] == base_id:
                        continue
                    s.execute(
                        "UPDATE attachment SET status='deleted', updated_at=? WHERE response_id=? AND attachment_id=?",
                        (now, response_id, r["attachment_id"]),
                    )
                result["kept"] = base_id

            elif row["conflict_type"] == "attachment_name":
                if resolution == "keep_incoming":
                    # 确认新附件归属：把旧同名附件软删除
                    old_id = detail["existing_id"]
                    s.execute(
                        "UPDATE attachment SET status='deleted', updated_at=? WHERE response_id=? AND attachment_id=?",
                        (now, response_id, old_id),
                    )
                    result["deleted"] = old_id
                elif resolution == "keep_existing":
                    new_id = detail["incoming_id"]
                    s.execute(
                        "UPDATE attachment SET status='deleted', updated_at=? WHERE response_id=? AND attachment_id=?",
                        (now, response_id, new_id),
                    )
                    result["deleted"] = new_id
                else:
                    return {"ok": False,
                            "reason": "同名附件冲突裁决必须是 keep_incoming / keep_existing"}

            elif row["conflict_type"] == "attachment_delete":
                if resolution == "confirm_delete":
                    result["note"] = "维持软删除，相关暂存版本一并标记删除"
                    base_like = row["attachment_id"]
                    for r in s.query(
                        "SELECT attachment_id FROM attachment WHERE response_id=? AND attachment_id LIKE ?",
                        (response_id, f"{base_like}%staged%"),
                    ):
                        s.execute(
                            "UPDATE attachment SET status='deleted', updated_at=? WHERE response_id=? AND attachment_id=?",
                            (now, response_id, r["attachment_id"]),
                        )
                elif resolution == "restore":
                    s.execute(
                        "UPDATE attachment SET status='present', updated_at=? WHERE response_id=? AND attachment_id=?",
                        (now, response_id, row["attachment_id"]),
                    )
                    result["restored"] = row["attachment_id"]
                else:
                    return {"ok": False,
                            "reason": "删除冲突裁决必须是 confirm_delete / restore"}

            elif row["conflict_type"] == "withdrawal":
                if restore_response:
                    s.execute("UPDATE response SET status='editing', updated_at=? WHERE id=?",
                              (now, response_id))
                    result["restored_response"] = True
                else:
                    result["note"] = "维持撤回，答卷保持终结状态"

            resolution_text = resolution
            chosen = chosen_value if chosen_value is not None else (chosen_attachment_id or "")
            if chosen != "":
                resolution_text = f"{resolution}|chosen={chosen}"
            s.execute(
                "UPDATE conflict SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                (resolution_text, now, conflict_id),
            )
            s.audit(response_id, "conflict.resolve",
                    f"{conflict_id} 人工裁决：{resolution}", None, None)
            self._recompute_status(response_id)
            s.commit()
            return result

    def _apply_branch(self, branch_id: str | None, conflict_id: str, now: str) -> dict:
        s = self.store
        if not branch_id:
            return {"ok": False, "reason": "分支不存在"}
        branch = s.query_one("SELECT * FROM branch WHERE id=?", (branch_id,))
        if branch is None:
            return {"ok": False, "reason": "分支不存在"}
        response_id = branch["response_id"]
        branch_fields = json.loads(branch["fields_json"])
        resp = s.query_one("SELECT head_rev FROM response WHERE id=?", (response_id,))
        new_rev = int(resp["head_rev"]) + 1
        for key, value in branch_fields.items():
            s.execute(
                "UPDATE response_field SET value=?, rev=?, origin_device='manual-branch'"
                " WHERE response_id=? AND field_key=?",
                (json.dumps(value, ensure_ascii=False), new_rev, response_id, key),
            )
            s.execute(
                "INSERT INTO field_revision(response_id,field_key,rev,value,origin_device,"
                "reason,changed_at) VALUES(?,?,?,?, 'manual-branch',?,?)",
                (response_id, key, new_rev, json.dumps(value, ensure_ascii=False),
                 f"整份分支应用:{branch_id}", now),
            )
        fp = fingerprint_of(branch_fields)
        s.execute(
            "INSERT INTO rev_fingerprint(response_id,rev,fingerprint,fields_json) VALUES(?,?,?,?)",
            (response_id, new_rev, fp, canonical_fields(branch_fields)),
        )
        s.execute("UPDATE response SET head_rev=?, updated_at=? WHERE id=?",
                  (new_rev, now, response_id))
        s.audit(response_id, "branch.apply",
                f"人工选择保留整份分支 {branch_id}，head_rev={new_rev}", None, None)
        return {"branch_applied": branch_id, "new_rev": new_rev}

    # ===================== 状态与发布门禁 =====================
    def _recompute_status(self, response_id: str) -> None:
        s = self.store
        resp = s.query_one("SELECT status FROM response WHERE id=?", (response_id,))
        if resp is None or resp["status"] == "withdrawn":
            return
        if resp["status"] == "published":
            return
        open_conflicts = s.query_one(
            "SELECT COUNT(*) AS c FROM conflict WHERE response_id=? AND status='open'",
            (response_id,),
        )["c"]
        if open_conflicts > 0:
            new_status = "conflict"
        elif self._attachment_completeness(response_id)["missing"]:
            new_status = "editing"
        else:
            new_status = "ready"
        s.execute("UPDATE response SET status=?, updated_at=? WHERE id=?",
                  (new_status, now_iso(), response_id))

    def _attachment_completeness(self, response_id: str) -> dict:
        s = self.store
        survey = self.get_survey(response_id)
        present = {r["slot"] for r in s.query(
            "SELECT DISTINCT slot FROM attachment WHERE response_id=? AND status='present'",
            (response_id,))}
        required = [a["slot"] for a in survey["attachments"] if a.get("required")]
        missing = [slot for slot in required if slot not in present]
        return {"required": required, "present_slots": sorted(present), "missing": missing}

    def publish(self, response_id: str) -> dict:
        s = self.store
        with s.lock:
            resp = s.query_one("SELECT * FROM response WHERE id=?", (response_id,))
            if resp is None:
                return {"ok": False, "reason": "答卷不存在"}
            blockers: list[str] = []
            if resp["status"] == "withdrawn":
                blockers.append("答卷已撤回，撤回未解除前禁止发布")
            open_conflicts = s.query(
                "SELECT id,basis,conflict_type FROM conflict WHERE response_id=? AND status='open'",
                (response_id,),
            )
            if open_conflicts:
                blockers.append(f"存在 {len(open_conflicts)} 条未裁决冲突")
            completeness = self._attachment_completeness(response_id)
            if completeness["missing"]:
                blockers.append(
                    "必交附件不齐套，缺失槽位：" + "、".join(completeness["missing"]))
            if blockers:
                s.audit(response_id, "publish.blocked", "；".join(blockers))
                s.commit()
                return {"ok": False, "blockers": blockers,
                        "completeness": completeness,
                        "open_conflicts": [dict(r) for r in open_conflicts]}
            s.execute("UPDATE response SET status='published', updated_at=? WHERE id=?",
                      (now_iso(), response_id))
            s.audit(response_id, "publish",
                    f"发布成功，head_rev={resp['head_rev']}，附件齐套且无未决冲突")
            s.commit()
            return {"ok": True, "published_at": now_iso(),
                    "head_rev": resp["head_rev"], "completeness": completeness}

    # ===================== 查询视图 =====================
    def get_survey(self, response_id: str) -> dict:
        s = self.store
        row = s.query_one(
            "SELECT definition FROM survey WHERE id=(SELECT survey_id FROM response WHERE id=?)",
            (response_id,),
        )
        return json.loads(row["definition"])

    def _integrity_of(self, receipt_id: str) -> str:
        return self.store.query_one(
            "SELECT integrity_status FROM raw_submission WHERE receipt_id=?", (receipt_id,)
        )["integrity_status"]

    def _note_of(self, receipt_id: str) -> str | None:
        return self.store.query_one(
            "SELECT note FROM raw_submission WHERE receipt_id=?", (receipt_id,)
        )["note"]

    def snapshot(self, response_id: str) -> dict:
        """工作台/设备共用的完整视图。"""
        s = self.store
        resp = s.query_one("SELECT * FROM response WHERE id=?", (response_id,))
        if resp is None:
            return {"error": "答卷不存在"}
        survey = self.get_survey(response_id)
        fields = []
        for r in s.query(
            "SELECT * FROM response_field WHERE response_id=? ORDER BY field_key",
            (response_id,),
        ):
            fields.append({"field_key": r["field_key"], "value": json.loads(r["value"]),
                           "rev": r["rev"], "origin_device": r["origin_device"]})
        fp_row = s.query_one(
            "SELECT fingerprint FROM rev_fingerprint WHERE response_id=? AND rev=?",
            (response_id, resp["head_rev"]),
        )
        attachments = [dict(r) for r in s.query(
            "SELECT * FROM attachment WHERE response_id=? ORDER BY slot, attachment_id",
            (response_id,))]
        conflicts = [dict(r) for r in s.query(
            "SELECT * FROM conflict WHERE response_id=? ORDER BY created_at", (response_id,))]
        for c in conflicts:
            c["detail"] = json.loads(c["detail"])
        branches = [dict(r) for r in s.query(
            "SELECT * FROM branch WHERE response_id=? ORDER BY created_at", (response_id,))]
        revisions = [dict(r) for r in s.query(
            "SELECT field_key,rev,value,origin_device,reason,changed_at"
            " FROM field_revision WHERE response_id=? ORDER BY id", (response_id,))]
        for r in revisions:
            r["value"] = json.loads(r["value"])
        cursors = [dict(r) for r in s.query(
            "SELECT * FROM device_cursor WHERE response_id=?", (response_id,))]
        completeness = self._attachment_completeness(response_id)
        return {
            "response": dict(resp),
            "survey": survey,
            "fields": fields,
            "head_fingerprint": fp_row["fingerprint"] if fp_row else None,
            "attachments": attachments,
            "conflicts": conflicts,
            "branches": branches,
            "revisions": revisions,
            "device_cursors": cursors,
            "completeness": completeness,
        }

    def receipts(self, response_id: str) -> list[dict]:
        return [dict(r) for r in self.store.query(
            "SELECT * FROM raw_submission WHERE response_id=? ORDER BY received_at",
            (response_id,))]

    def queue(self, response_id: str) -> list[dict]:
        rows = [dict(r) for r in self.store.query(
            "SELECT * FROM op_queue WHERE response_id=? ORDER BY seq", (response_id,))]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
            r["result"] = json.loads(r["result"]) if r["result"] else None
        return rows

    def audit_log(self, response_id: str | None = None) -> list[dict]:
        if response_id:
            rows = self.store.query(
                "SELECT * FROM audit_log WHERE response_id=? ORDER BY id", (response_id,))
        else:
            rows = self.store.query("SELECT * FROM audit_log ORDER BY id")
        return [dict(r) for r in rows]

    def replay_pending(self) -> dict:
        """重启后重放队列里仍 pending 的任务（FIFO）。"""
        processed = []
        while True:
            r = self._process_next()
            if not r.get("processed"):
                break
            processed.append(r)
        return {"replayed": processed}

    def attachment_bytes(self, response_id: str, attachment_id: str):
        s = self.store
        row = s.query_one(
            "SELECT * FROM attachment WHERE response_id=? AND attachment_id=?",
            (response_id, attachment_id),
        )
        if row is None or row["status"] != "present":
            return None
        with open(s.blob_path(row["content_hash"]), "rb") as f:
            return row["file_name"], row["content_hash"], f.read()
