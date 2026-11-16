"""端到端测试：用临时目录构造 Engine，覆盖全部裁决规则。

运行（按环境约定使用 -I 隔离模式）：
    python -I -m unittest -v tests.test_engine
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.engine import Engine, fingerprint_of, canonical_fields
from server.store import Store, short_id

RESP = "RSP-DEMO-01"


def env(kind, device, op_id=None, **kw):
    out = {
        "submission_id": short_id("SUB"),
        "response_id": RESP,
        "device_id": device,
        "kind": kind,
        "op_id": op_id or short_id("OP"),
    }
    out.update(kw)
    return out


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="survey-test-")
        self.store = Store(os.path.join(self.tmp, "t.db"),
                           os.path.join(self.tmp, "blobs"))
        self.engine = Engine(self.store)
        self.engine.seed()
        self.snap0 = self.engine.snapshot(RESP)
        self.fp0 = self.snap0["head_fingerprint"]
        self.fields0 = {f["field_key"]: f["value"] for f in self.snap0["fields"]}

    def tearDown(self):
        self.store.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def answers(self, device, changed, base_rev=0, base_fp=None, op_id=None,
                stale=False):
        # delta 协议：只回传发生变化的字段。
        # stale=True 表示设备仍带着 base_rev 这一旧版的指纹离线编辑（头部已被别人推进），
        # 用于检验 3-way 合并能否识别分叉；默认取 base_rev 对应的真实指纹。
        if base_fp is None:
            fp_rev = base_rev if stale else self.engine.snapshot(RESP)["response"]["head_rev"]
            base_fp = self.store.query_one(
                "SELECT fingerprint FROM rev_fingerprint WHERE response_id=? AND rev=?",
                (RESP, fp_rev),
            )["fingerprint"]
        return self.engine.receive(env(
            "answers", device, op_id=op_id,
            base_rev=base_rev, base_fingerprint=base_fp,
            fields=dict(changed),
        ))

    def status(self):
        return self.engine.snapshot(RESP)["response"]["status"]

    def field_value(self, key):
        snap = self.engine.snapshot(RESP)
        return next(f["value"] for f in snap["fields"] if f["field_key"] == key)


class TestBasicMerge(EngineTestBase):
    def test_01_receipt_is_only_storage_ack(self):
        r = self.answers("A", {"customer": "甲方公司"})
        self.assertEqual(r["integrity_status"], "valid")
        self.assertIn("receipt_id", r)
        # 收据之后有独立的处理结果
        self.assertEqual(r["process_result"]["state"], "applied")
        receipts = self.engine.receipts(RESP)
        self.assertEqual(len(receipts), 1)
        # 原始报文完整存证
        self.assertEqual(json.loads(receipts[0]["payload"])["device_id"], "A")

    def test_02_fast_forward_and_cursor(self):
        self.answers("A", {"customer": "甲方公司"})
        snap = self.engine.snapshot(RESP)
        self.assertEqual(snap["response"]["head_rev"], 1)
        cursor = next(c for c in snap["device_cursors"] if c["device_id"] == "A")
        self.assertEqual(cursor["base_rev"], 1)

    def test_03_duplicate_submission_idempotent(self):
        e = env("answers", "A", base_rev=0, base_fingerprint=self.fp0,
                fields={**self.fields0, "customer": "甲"})
        first = self.engine.receive(e)
        second = self.engine.receive(dict(e))  # 同一 submission_id
        self.assertEqual(second["integrity_status"], "duplicate")
        self.assertFalse(second["queued"])
        self.assertEqual(second["duplicate_of"], first["receipt_id"])
        self.assertEqual(len(self.engine.queue(RESP)), 1)  # 没有第二条入队

    def test_04_client_op_id_idempotent(self):
        op = "OP-FIXED-1"
        r1 = self.answers("A", {"customer": "甲"}, op_id=op)
        # 换 submission_id 但 op_id 相同：入队但幂等不生效
        r2 = self.answers("A", {"customer": "乙"}, op_id=op)
        self.assertEqual(r2["process_result"]["state"], "idempotent")
        self.assertEqual(self.field_value("customer"), "甲")


class TestThreeWayMerge(EngineTestBase):
    def test_05_distinct_fields_auto_merge(self):
        # A 改 customer，B 改 comment：不同字段，自动合并。
        # B 先同步到 rev1（基准指纹随之更新），再在 rev1 基础上改 comment
        self.answers("A", {"customer": "甲方"})
        r_b = self.answers("B", {"comment": "现场整洁"}, base_rev=1)
        self.assertEqual(r_b["process_result"]["state"], "applied")
        self.assertEqual(self.field_value("customer"), "甲方")
        self.assertEqual(self.field_value("comment"), "现场整洁")

    def test_06_same_field_same_value_converges(self):
        # B 基于旧 rev0 离线，两边都把 satisfaction 改成 5 → 收敛，自动落修订
        self.answers("A", {"satisfaction": "5"})
        r_b = self.answers("B", {"satisfaction": "5"}, base_rev=0, stale=True)
        self.assertEqual(r_b["process_result"]["auto_applied"][0]["rule"], "converged")

    def test_07_same_field_diverges_conflict_and_no_overwrite(self):
        # B 基于旧 rev0 离线，双方都改 customer 且值不同 → 字段冲突
        self.answers("A", {"customer": "甲方-A"})
        r_b = self.answers("B", {"customer": "甲方-B"}, base_rev=0, stale=True)
        self.assertEqual(r_b["process_result"]["state"], "conflicted")
        # 旧答卷不能盖新修改：服务端保留 A 的值
        self.assertEqual(self.field_value("customer"), "甲方-A")
        conflict = self.engine.snapshot(RESP)["conflicts"][0]
        self.assertEqual(conflict["conflict_type"], "field")
        self.assertEqual(conflict["status"], "open")
        self.assertIn("三方分叉", conflict["basis"])  # 页面展示的裁决依据

    def test_08_stale_base_keeps_old_value_without_silent_overwrite(self):
        # B 基于 rev=0 离线编辑，期间 A 已提交 rev=1
        self.answers("A", {"customer": "甲方-A"})
        r_b = self.answers("B", {"customer": "甲方-B"})  # 仍带 rev=0 + 正确 fp0
        self.assertEqual(r_b["process_result"]["state"], "conflicted")
        self.assertEqual(self.field_value("customer"), "甲方-A")

    def test_09_manual_field_resolution_writes_revision(self):
        self.answers("A", {"customer": "甲方-A"})
        r_b = self.answers("B", {"customer": "甲方-B"}, base_rev=0, stale=True)
        cid = r_b["process_result"]["conflicts"][0]["conflict_id"]
        res = self.engine.resolve_conflict(cid, "manual", chosen_value="甲方-人工裁定")
        self.assertTrue(res["ok"])
        self.assertEqual(self.field_value("customer"), "甲方-人工裁定")
        # 重复裁决被拒绝
        again = self.engine.resolve_conflict(cid, "manual", chosen_value="x")
        self.assertFalse(again["ok"])


class TestFingerprint(EngineTestBase):
    def test_10_fingerprint_mismatch_preserves_branch(self):
        r = self.engine.receive(env(
            "answers", "A", base_rev=0,
            base_fingerprint="f" * 64,  # 故意错误指纹
            fields={**self.fields0, "customer": "篡改基准后的修改"},
        ))
        self.assertEqual(r["integrity_status"], "fingerprint_mismatch")
        self.assertEqual(r["process_result"]["state"], "conflicted")
        snap = self.engine.snapshot(RESP)
        # 服务端数据纹丝不动
        self.assertEqual(self.field_value("customer"), "")
        self.assertEqual(len(snap["branches"]), 1)
        conflict = next(c for c in snap["conflicts"] if c["conflict_type"] == "fingerprint")
        self.assertEqual(conflict["status"], "open")

    def test_11_apply_and_discard_branch(self):
        r = self.engine.receive(env(
            "answers", "A", base_rev=0, base_fingerprint="f" * 64,
            fields={**self.fields0, "customer": "分支值"},
        ))
        cid = r["process_result"]["conflict_id"]
        branch = r["process_result"]["branch_id"]
        self.engine.resolve_conflict(cid, "apply_branch")
        self.assertEqual(self.field_value("customer"), "分支值")

        # 再来一个指纹冲突并选择丢弃
        r2 = self.engine.receive(env(
            "answers", "B", base_rev=self.engine.snapshot(RESP)["response"]["head_rev"],
            base_fingerprint="0" * 64,
            fields={**self.fields0, "customer": "应被丢弃"},
        ))
        before = self.field_value("customer")
        self.engine.resolve_conflict(r2["process_result"]["conflict_id"], "discard_branch")
        self.assertEqual(self.field_value("customer"), before)
        self.assertEqual(len(self.engine.snapshot(RESP)["branches"]), 2)


class TestWithdrawFifo(EngineTestBase):
    def test_12_withdraw_blocks_later_edits(self):
        # 队列顺序：answers(待处理场景下) -> withdraw -> answers
        wr = self.engine.receive(env("withdraw", "A", reason="客户取消"))
        self.assertEqual(wr["process_result"]["state"], "applied")
        self.assertEqual(self.status(), "withdrawn")
        later = self.answers("B", {"customer": "迟到的修改"})
        self.assertEqual(later["process_result"]["state"], "blocked")
        self.assertIn("撤回", later["process_result"]["reason"])
        # 队列里可看到 blocked 记录
        q = self.engine.queue(RESP)
        self.assertEqual(q[-1]["state"], "blocked")

    def test_13_old_merge_cannot_overtake_new_withdrawal(self):
        # 严格按到达顺序入队处理：seq1 旧编辑 applied → seq2 撤回 applied →
        # seq3 迟到的旧编辑 blocked。验证撤回之后没有任何旧修改能落到答卷上。
        self.answers("B", {"customer": "撤回前的合法修改"})
        self.engine.receive(env("withdraw", "A"))
        stale = self.answers("B", {"customer": "撤回后的迟到修改"})
        self.assertEqual(stale["process_result"]["state"], "blocked")
        self.assertEqual(self.field_value("customer"), "撤回前的合法修改")
        q = self.engine.queue(RESP)
        self.assertEqual([r["seq"] for r in q], [1, 2, 3])
        self.assertEqual([r["state"] for r in q],
                         ["applied", "applied", "blocked"])
        # 没有遗留 pending；重放为空操作
        self.assertEqual(self.engine.replay_pending()["replayed"], [])
        self.assertEqual(self.status(), "withdrawn")

    def test_14_restore_after_withdrawal(self):
        wr = self.engine.receive(env("withdraw", "A"))
        cid = wr["process_result"]["conflict_id"]
        self.engine.resolve_conflict(cid, "restore", restore_response=True)
        self.assertIn(self.status(), ("editing", "ready"))


class TestAttachments(EngineTestBase):
    def _att(self, name="photo.bin", slot="id_photo", content=b"hello", aid=None):
        return {
            "attachment_id": aid or short_id("ATT"),
            "slot": slot, "file_name": name,
            "content_base64": base64.b64encode(content).decode(),
        }

    def test_15_attach_content_addressing_and_dedup(self):
        att = self._att()
        r1 = self.engine.receive(env("attach", "A", attachment=att))
        self.assertEqual(r1["process_result"]["state"], "applied")
        # 同 ID 同内容重传：幂等
        r2 = self.engine.receive(env("attach", "A", attachment=dict(att),
                                     op_id=short_id("OP")))
        self.assertEqual(r2["process_result"]["state"], "idempotent")
        # blob 可下载
        got = self.engine.attachment_bytes(RESP, att["attachment_id"])
        self.assertEqual(got[2], b"hello")

    def test_16_same_id_different_content_version_conflict(self):
        att = self._att(content=b"v1")
        self.engine.receive(env("attach", "A", attachment=att))
        att2 = dict(att); att2["content_base64"] = base64.b64encode(b"v2").decode()
        r = self.engine.receive(env("attach", "B", attachment=att2))
        self.assertEqual(r["process_result"]["state"], "conflicted")
        snap = self.engine.snapshot(RESP)
        conflict = next(c for c in snap["conflicts"]
                        if c["conflict_type"] == "attachment_version")
        # 旧版本未被覆盖
        old = next(a for a in snap["attachments"] if a["attachment_id"] == att["attachment_id"])
        self.assertEqual(old["status"], "present")
        # 人工采用新版本
        staged = r["process_result"]["staged_attachment_id"]
        res = self.engine.resolve_conflict(conflict["id"], "manual",
                                           chosen_attachment_id=staged)
        self.assertTrue(res["ok"])
        snap2 = self.engine.snapshot(RESP)
        kept = next(a for a in snap2["attachments"]
                    if a["attachment_id"] == att["attachment_id"])
        self.assertEqual(kept["status"], "present")

    def test_17_same_name_different_id_conflict(self):
        a1 = self._att(name="same.jpg", aid=short_id("ATT"))
        a2 = self._att(name="same.jpg", aid=short_id("ATT"), content=b"other")
        self.engine.receive(env("attach", "A", attachment=a1))
        r = self.engine.receive(env("attach", "B", attachment=a2))
        self.assertEqual(r["process_result"]["state"], "conflicted")
        cid = r["process_result"]["conflict_id"]
        self.engine.resolve_conflict(cid, "keep_existing")
        snap = self.engine.snapshot(RESP)
        deleted = next(a for a in snap["attachments"]
                       if a["attachment_id"] == a2["attachment_id"])
        self.assertEqual(deleted["status"], "deleted")

    def test_18_delete_is_tombstone_and_conflicts_with_version(self):
        att = self._att(content=b"v1")
        self.engine.receive(env("attach", "A", attachment=att))
        att2 = dict(att); att2["content_base64"] = base64.b64encode(b"v2").decode()
        self.engine.receive(env("attach", "B", attachment=att2))  # 版本冲突未决
        r = self.engine.receive(env("delete_attach", "A",
                                    attachment_id=att["attachment_id"]))
        self.assertEqual(r["process_result"]["state"], "conflicted")
        # 软删除后下载入口关闭，但 blob 文件仍在磁盘上可恢复
        self.assertIsNone(self.engine.attachment_bytes(RESP, att["attachment_id"]))
        row = next(a for a in self.engine.snapshot(RESP)["attachments"]
                   if a["attachment_id"] == att["attachment_id"])
        self.assertTrue(os.path.exists(self.store.blob_path(row["content_hash"])))
        self.engine.resolve_conflict(r["process_result"]["conflict_id"], "restore")
        snap = self.engine.snapshot(RESP)
        row = next(a for a in snap["attachments"]
                   if a["attachment_id"] == att["attachment_id"])
        self.assertEqual(row["status"], "present")

    def test_19_attach_blocked_after_withdrawal(self):
        self.engine.receive(env("withdraw", "A"))
        r = self.engine.receive(env("attach", "B", attachment=self._att()))
        self.assertEqual(r["process_result"]["state"], "blocked")


class TestPublishGate(EngineTestBase):
    def _attach(self, slot, name, content):
        return {
            "attachment_id": short_id("ATT"), "slot": slot,
            "file_name": name,
            "content_base64": base64.b64encode(content).decode(),
        }

    def test_20_publish_blocked_by_missing_attachments_and_conflicts(self):
        # 无附件、无冲突：门禁应报缺附件
        pub = self.engine.publish(RESP)
        self.assertFalse(pub["ok"])
        self.assertTrue(any("齐套" in b for b in pub["blockers"]))

        self.engine.receive(env("attach", "A",
                                attachment=self._attach("id_photo", "p.bin", b"1")))
        self.engine.receive(env("attach", "A",
                                attachment=self._attach("signature", "s.bin", b"2")))
        # 制造一个字段冲突（双方基于同一旧基准，同改一个字段且值不同）
        self.answers("A", {"customer": "甲"})
        self.answers("B", {"customer": "乙"}, base_rev=0, stale=True)
        pub2 = self.engine.publish(RESP)
        self.assertFalse(pub2["ok"])
        self.assertTrue(any("冲突" in b for b in pub2["blockers"]))

    def test_21_full_happy_path_publishes(self):
        self.answers("A", {"customer": "甲方", "satisfaction": "5"})
        self.engine.receive(env("attach", "A",
                                attachment=self._attach("id_photo", "p.bin", b"1")))
        self.engine.receive(env("attach", "B",
                                attachment=self._attach("signature", "s.bin", b"2")))
        self.assertEqual(self.status(), "ready")
        pub = self.engine.publish(RESP)
        self.assertTrue(pub["ok"])
        self.assertEqual(self.status(), "published")

    def test_22_withdrawn_cannot_publish(self):
        self.engine.receive(env("withdraw", "A"))
        pub = self.engine.publish(RESP)
        self.assertFalse(pub["ok"])
        self.assertTrue(any("撤回" in b for b in pub["blockers"]))


class TestRestartReplay(unittest.TestCase):
    def test_24_pending_queue_replayed_fifo_on_restart(self):
        tmp = tempfile.mkdtemp(prefix="survey-restart-")
        db = os.path.join(tmp, "t.db")
        blob = os.path.join(tmp, "blobs")
        try:
            # 第一个进程：制造一条入队但未处理的任务（直接构造 pending）
            st1 = Store(db, blob)
            eng1 = Engine(st1)
            eng1.seed()
            fp0 = eng1.snapshot(RESP)["head_fingerprint"]
            envelope = env("answers", "A", base_rev=0, base_fingerprint=fp0,
                           fields={"customer": "重启前积压"})
            st1.execute(
                "INSERT INTO raw_submission(receipt_id,client_submission_id,device_id,"
                "response_id,kind,payload,received_at,integrity_status,duplicate_of,note)"
                " VALUES('RCP-R1',?, 'A',?,'answers',?,?,'valid',NULL,NULL)",
                (envelope["submission_id"], RESP, json.dumps(envelope), "2026-01-01T00:00:00+00:00"),
            )
            st1.execute(
                "INSERT INTO op_queue(receipt_id,response_id,device_id,op_type,state,"
                "client_op_id,payload) VALUES('RCP-R1',?,'A','answers','pending',?,?)",
                (RESP, envelope["op_id"], json.dumps(envelope)),
            )
            st1.commit()
            st1.conn.close()

            # 第二个进程：模拟重启，构造时应自动重放 pending
            st2 = Store(db, blob)
            eng2 = Engine(st2)
            replay = eng2.replay_pending()
            self.assertEqual(len(replay["replayed"]), 1)
            self.assertEqual(replay["replayed"][0]["state"], "applied")
            snap = eng2.snapshot(RESP)
            self.assertEqual(
                next(f["value"] for f in snap["fields"] if f["field_key"] == "customer"),
                "重启前积压")
            # 再次重放为空操作
            self.assertEqual(eng2.replay_pending()["replayed"], [])
            st2.conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFingerprintAlgo(unittest.TestCase):
    def test_23_canonical_stable(self):
        a = {"b": 1, "a": "x"}
        b = {"a": "x", "b": 1}
        self.assertEqual(canonical_fields(a), canonical_fields(b))
        self.assertEqual(len(fingerprint_of(a)), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
