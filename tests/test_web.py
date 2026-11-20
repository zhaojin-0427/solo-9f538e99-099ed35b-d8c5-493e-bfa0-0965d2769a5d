"""网页流程测试：上传、预览、校订、发布、下载、过期页与失败原子性。"""
from __future__ import annotations

import hashlib
import re

import pytest

from app import core
from app.app import create_app

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:05.000
第一句原话。

2
00:00:05.000 --> 00:00:10.000
第二句原话，比较长，
分成两行。

3
00:00:10.000 --> 00:00:15.000
第三句原话。
"""

CSV = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:12.000
C,00:00:12.000,00:00:15.000
"""


@pytest.fixture()
def app(tmp_path):
    application = create_app(str(tmp_path / "test.db"))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, vtt=VTT, csv=CSV):
    response = client.post(
        "/upload",
        data={
            "vtt": (open_bytes(vtt), "interview.vtt"),
            "csv": (open_bytes(csv), "edit.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.data.decode()
    match = re.search(r"/draft/(\d+)", response.headers["Location"])
    assert match, response.headers["Location"]
    return int(match.group(1))


def open_bytes(text: str):
    import io

    return io.BytesIO(text.encode("utf-8"))


def draft_form(client, draft_id):
    """读取草稿页，解析出隐藏的版本号与每个剪口出现的输入项。"""
    page = client.get(f"/draft/{draft_id}")
    html = page.data.decode()
    revision = re.search(r'name="revision" value="(\d+)"', html).group(1)
    return page, revision, html


def occurrence_fields(html):
    return sorted({int(m) for m in re.findall(r'name="status:(\d+)"', html)})


def fill(client, draft_id, decider, publish=False):
    page, revision, html = draft_form(client, draft_id)
    data = {"revision": revision}
    for index in occurrence_fields(html):
        status, text = decider(index)
        data[f"status:{index}"] = status
        data[f"text:{index}"] = text
    url = f"/draft/{draft_id}/" + ("publish" if publish else "save")
    return client.post(url, data=data, follow_redirects=False)


class TestUploadAndPreview:
    def test_upload_creates_draft_and_previews_both_timelines(self, client):
        draft_id = upload(client)
        page, revision, html = draft_form(client, draft_id)
        assert page.status_code == 200
        # 每一次出现都同时给出原片与节选时码
        assert "原片" in html and "节选" in html
        assert "00:00:01.000" in html  # 原片起点
        assert "00:00:00.000" in html  # 节选起点
        # 片段 B 的节选偏移 = A 的 4s
        assert "00:00:04.000" in html
        # 剪口截断的条目需要录入
        assert occurrence_fields(html)

    def test_bad_upload_reports_error(self, client):
        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes("not a vtt"), "x.vtt"),
                "csv": (open_bytes(CSV), "x.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "上传失败" in response.data.decode()

    def test_duplicate_fragment_number_rejected(self, client):
        bad_csv = "片段号,原片起毫秒,原片止毫秒\nA,0,1000\nA,1000,2000\n"
        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes(VTT), "x.vtt"),
                "csv": (open_bytes(bad_csv), "x.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "片段号重复" in response.data.decode()


class TestReviewAndPublish:
    def test_cannot_publish_until_every_cut_handled(self, client):
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        assert fields
        # 只处理第一处就发布会失败
        data = {"revision": revision, f"status:{fields[0]}": "delete"}
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422
        assert "未处理" in response.data.decode()
        # 失败不产生新版
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0

    def test_full_publish_and_download(self, client):
        draft_id = upload(client)
        response = fill(
            client,
            draft_id,
            lambda index: ("keep", f"保留文字{index}"),
            publish=True,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "/editions/1" in location

        download = client.get("/editions/1/download")
        assert download.status_code == 200
        assert download.headers["Content-Type"].startswith("text/vtt")
        body = download.data.decode("utf-8")
        assert body.startswith("WEBVTT")
        assert body.endswith("\n")

        parsed = core.parse_vtt(body)
        texts = [cue.text for cue in parsed]
        assert "第一句原话。" in texts  # 整条保留沿用正文
        # 剪口被截断的每一次出现都按各自录入的文字输出
        assert "保留文字1" in texts
        assert "保留文字3" in texts
        assert "保留文字4" in texts
        assert len([t for t in texts if t.startswith("保留文字")]) == 3

    def test_deleted_cut_occurrence_absent_from_output(self, client):
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        body = client.get("/editions/1/download").data.decode()
        parsed = core.parse_vtt(body)
        # 被删掉的剪口不出现，但整条保留的仍在
        assert "第一句原话。" in [cue.text for cue in parsed]
        assert body.count("-->") == len(parsed)

    def test_publish_twice_from_same_page_is_rejected(self, client):
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        data = {"revision": revision}
        for index in occurrence_fields(html):
            data[f"status:{index}"] = "delete"
        first = client.post(f"/draft/{draft_id}/publish", data=data)
        assert first.status_code == 302
        second = client.post(f"/draft/{draft_id}/publish", data=data)
        assert second.status_code == 409
        assert "过期" in second.data.decode()


class TestStaleDraft:
    def test_old_page_rejected_after_new_upload(self, client):
        old_id = upload(client)
        _, old_revision, old_html = draft_form(client, old_id)
        fields = occurrence_fields(old_html)

        new_id = upload(client, csv=CSV.replace("00:00:12.000", "00:00:11.000"))
        assert new_id != old_id

        data = {"revision": old_revision}
        for index in fields:
            data[f"status:{index}"] = "delete"
        response = client.post(f"/draft/{old_id}/save", data=data)
        assert response.status_code == 409
        assert "刷新" in response.data.decode()

    def test_old_page_rejected_on_publish(self, client):
        old_id = upload(client)
        _, old_revision, old_html = draft_form(client, old_id)
        fields = occurrence_fields(old_html)
        upload(client)  # 更换剪辑表

        data = {"revision": old_revision}
        for index in fields:
            data[f"status:{index}"] = "delete"
        response = client.post(f"/draft/{old_id}/publish", data=data)
        assert response.status_code == 409
        assert "过期" in response.data.decode()
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0


class TestRecomputeAndInheritance:
    def test_new_csv_recomputes_excerpt_times(self, client):
        draft_id = upload(client)
        _, _, old_html = draft_form(client, draft_id)
        # 原表：A 4s + B 9s => C 的节选从 13s 开始
        assert "00:00:13.000" in old_html
        fill(client, draft_id, lambda index: ("keep", f"校订{index}"), publish=False)
        # 把 B 剪短 1 秒：C 的节选时码整体前移 1 秒
        new_csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:11.000
C,00:00:12.000,00:00:15.000
"""
        new_id = upload(client, csv=new_csv)
        _, _, html = draft_form(client, new_id)
        # A 4s + B 8s = 12s，C 的节选为 12s–15s
        assert "00:00:12.000" in html
        assert "00:00:15.000" in html
        assert "00:00:13.000" not in html

    def test_inheritance_requires_fragment_text_and_range(self, client):
        review_csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:12.000
C,00:00:12.000,00:00:15.000
"""
        old_id = upload(client, csv=review_csv)
        _, revision, html = draft_form(client, old_id)
        fields = occurrence_fields(html)
        # A/C 各有一处剪口，B 的起止两端各截断一条字幕
        assert len(fields) == 3
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"旧校订{index}"
        assert client.post(f"/draft/{old_id}/save", data=data).status_code == 302

        # 同一份剪辑表：三处剪口全部沿用
        same_id = upload(client, csv=review_csv)
        same_html = draft_form(client, same_id)[2]
        assert same_html.count("沿用上一版校订") == 3
        assert "旧校订" in same_html

        # 只改变 B 的止毫秒：B 里结束端被截的那条原片范围变了，不再沿用
        changed = review_csv.replace(
            "00:00:03.000,00:00:12.000", "00:00:03.000,00:00:11.000"
        )
        changed_id = upload(client, csv=changed)
        changed_html = draft_form(client, changed_id)[2]
        assert changed_html.count("沿用上一版校订") == 2

    def test_changed_original_text_loses_inheritance(self, client):
        old_csv = "片段号,原片起毫秒,原片止毫秒\nA,00:00:03.000,00:00:08.000\n"
        old_id = upload(client, csv=old_csv)
        revision = draft_form(client, old_id)[1]
        fields = occurrence_fields(draft_form(client, old_id)[2])
        assert len(fields) == 2  # cue1 起端截断、cue2 末端截断
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"旧校订{index}"
        client.post(f"/draft/{old_id}/save", data=data)

        # 改掉其中一条原字幕的文字：只有这一条失去沿用
        new_vtt = VTT.replace("第二句原话，比较长，", "第二句原话，改过，")
        new_id = upload(client, csv=old_csv, vtt=new_vtt)
        new_html = draft_form(client, new_id)[2]
        assert new_html.count("沿用上一版校订") == 1

    def test_same_span_taken_twice_needs_two_entries(self, client):
        csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:03.000,00:00:12.000
A2,00:00:03.000,00:00:12.000
"""
        draft_id = upload(client, csv=csv)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        # 两段相同的原片范围各出现一次，需分别处理
        assert len(fields) >= 2
        data = {"revision": revision, f"status:{fields[0]}": "delete"}
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422


class TestPersistence:
    def test_edition_survives_restart(self, tmp_path):
        db = str(tmp_path / "restart.db")
        first = create_app(db)
        first.config.update(TESTING=True)
        client = first.test_client()
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        expected = client.get("/editions/1/download").data

        second = create_app(db)  # 模拟重启
        second.config.update(TESTING=True)
        again = second.test_client().get("/editions/1/download")
        assert again.status_code == 200
        assert again.data == expected

    def test_failed_publish_leaves_no_version(self, tmp_path):
        db = str(tmp_path / "atomic.db")
        application = create_app(db)
        application.config.update(TESTING=True)
        client = application.test_client()
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        data = {"revision": revision}
        for index in fields[:-1]:
            data[f"status:{index}"] = "delete"
        # 最后一处留空，发布应被拒绝
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422

        conn = core.connect(db)
        try:
            assert core.get_edition(conn, 1) is None
        finally:
            conn.close()


# 两人同时说话：重叠字幕 + REGION 分区 + 时间行设置
VTT_OVERLAP = """WEBVTT

REGION
id:upper
lines:2
viewportanchor:50%,0%

REGION
id:lower
lines:2

REGION
id:unused
lines:1

1
00:00:01.000 --> 00:00:05.000 region:upper line:10%
第一句原话。

2
00:00:03.000 --> 00:00:07.000 region:lower line:90%
重叠的第二句。

3
00:00:08.000 --> 00:00:12.000
第三句原话。
"""

# 引用 upper 但没有它的 REGION 定义
VTT_GHOST = """WEBVTT

REGION
id:lower
lines:2

1
00:00:01.000 --> 00:00:05.000 region:upper line:10%
第一句原话。

2
00:00:03.000 --> 00:00:07.000 region:lower line:90%
重叠的第二句。

3
00:00:08.000 --> 00:00:12.000
第三句原话。
"""

CSV_OVERLAP = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.500,00:00:12.000
"""


@pytest.fixture()
def app_db(tmp_path):
    db = str(tmp_path / "layout.db")
    application = create_app(db)
    application.config.update(TESTING=True)
    return application, db


def publish_overlap(client, vtt=VTT_OVERLAP):
    """上传重叠字幕并发布：cue1 起端被截，按 keep 处理。"""
    draft_id = upload(client, vtt=vtt, csv=CSV_OVERLAP)
    response = fill(
        client, draft_id, lambda index: ("keep", "第一句的保留文字"), publish=True
    )
    assert response.status_code == 302, response.data.decode()
    return draft_id


def make_legacy_edition(client, db):
    """模拟布局丢失的旧版：发布后把成品换成剥掉设置与 REGION 的字节。"""
    publish_overlap(client)
    conn = core.connect(db)
    try:
        edition = core.get_edition(conn, 1)
        cues = core.parse_vtt(edition["vtt_blob"].decode("utf-8"))
        stripped = core.build_vtt(
            [(cue.start_ms, cue.end_ms, cue.text.split("\n")) for cue in cues]
        )
        blob = stripped.encode("utf-8")
        conn.execute(
            "UPDATE edition SET vtt_blob = ?, vtt_sha256 = ? WHERE version_no = 1",
            (blob, hashlib.sha256(blob).hexdigest()),
        )
    finally:
        conn.close()
    return stripped


class TestLayoutPreservation:
    def test_publish_keeps_settings_and_referenced_regions(self, client):
        publish_overlap(client)
        body = client.get("/editions/1/download").data.decode()
        # 每条字幕保留对应原字幕的时间行设置（剪口校订的那条也一样）
        assert "00:00:00.000 --> 00:00:03.500 region:upper line:10%" in body
        assert "00:00:01.500 --> 00:00:05.500 region:lower line:90%" in body
        # 成品只收录实际引用的 REGION 定义
        assert "id:upper" in body and "id:lower" in body
        assert "id:unused" not in body
        # REGION 块排在字幕之前
        assert body.index("REGION") < body.index("-->")
        # 原件正文不受影响
        parsed = core.parse_vtt(body)
        assert [cue.text for cue in parsed] == [
            "第一句的保留文字",
            "重叠的第二句。",
            "第三句原话。",
        ]

    def test_draft_page_shows_occurrence_settings(self, client):
        draft_id = upload(client, vtt=VTT_OVERLAP, csv=CSV_OVERLAP)
        html = draft_form(client, draft_id)[2]
        assert "时间行设置" in html
        assert "region:upper line:10%" in html
        assert "region:lower line:90%" in html

    def test_missing_region_marked_and_publish_rejected(self, client):
        draft_id = upload(client, vtt=VTT_GHOST, csv=CSV_OVERLAP)
        html = draft_form(client, draft_id)[2]
        # 在对应字幕旁标明缺少的定义
        assert "缺少 REGION 定义：upper" in html
        # 直接提交发布也被拒绝，且不留新版本
        response = fill(
            client, draft_id, lambda index: ("keep", "保留文字"), publish=True
        )
        assert response.status_code == 422
        assert "未定义的 REGION" in response.data.decode()
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0

    def test_deleted_occurrence_with_missing_region_can_publish(self, client):
        draft_id = upload(client, vtt=VTT_GHOST, csv=CSV_OVERLAP)
        # 引用缺失定义的那次出现被标为删除：不进入成品，不再阻止发布
        response = fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        assert response.status_code == 302
        body = client.get("/editions/1/download").data.decode()
        assert "id:lower" in body
        assert "region:upper" not in body


class TestLayoutRepair:
    def test_repair_restores_layout_and_keeps_original(self, app_db):
        application, db = app_db
        client = application.test_client()
        legacy = make_legacy_edition(client, db)
        assert "REGION" not in legacy and "region:upper" not in legacy
        original_sha = hashlib.sha256(legacy.encode("utf-8")).hexdigest()

        # 详情页保留原 SHA-256
        detail = client.get("/editions/1").data.decode()
        assert original_sha in detail

        response = client.post("/editions/1/repair", follow_redirects=False)
        assert response.status_code == 302

        repaired = client.get("/editions/1/repaired")
        assert repaired.status_code == 200
        body = repaired.data.decode()
        assert "id:upper" in body and "id:lower" in body
        assert "id:unused" not in body
        assert "00:00:00.000 --> 00:00:03.500 region:upper line:10%" in body

        # 原下载地址仍返回原件，详情页仍显示原 SHA-256
        assert client.get("/editions/1/download").data.decode() == legacy
        detail = client.get("/editions/1").data.decode()
        assert original_sha in detail
        assert hashlib.sha256(repaired.data).hexdigest() in detail

    def test_repair_once_survives_cleanup_and_restart(self, tmp_path):
        db = str(tmp_path / "repair.db")
        application = create_app(db)
        application.config.update(TESTING=True)
        client = application.test_client()
        make_legacy_edition(client, db)

        client.post("/editions/1/repair")
        first = client.get("/editions/1/repaired").data
        # 重复修复：每版只留一份，字节不变
        client.post("/editions/1/repair")
        assert client.get("/editions/1/repaired").data == first

        # 清理源草稿（校订级联删除）后仍能下载相同字节
        conn = core.connect(db)
        try:
            conn.execute("DELETE FROM draft")
        finally:
            conn.close()
        assert client.get("/editions/1/repaired").data == first

        # 重启后仍能下载相同字节
        restarted = create_app(db)
        restarted.config.update(TESTING=True)
        again = restarted.test_client().get("/editions/1/repaired")
        assert again.status_code == 200
        assert again.data == first

    def test_repair_fails_when_source_draft_missing(self, app_db):
        application, db = app_db
        client = application.test_client()
        make_legacy_edition(client, db)
        conn = core.connect(db)
        try:
            conn.execute("DELETE FROM draft")
        finally:
            conn.close()

        response = client.post("/editions/1/repair", follow_redirects=True)
        assert "源草稿" in response.data.decode()
        assert client.get("/editions/1/repaired").status_code == 404
        # 不留下修复件
        conn = core.connect(db)
        try:
            assert core.get_edition(conn, 1)["repaired_blob"] is None
        finally:
            conn.close()

    def test_repair_fails_when_text_mismatches(self, app_db):
        application, db = app_db
        client = application.test_client()
        make_legacy_edition(client, db)
        # 篡改源草稿的校订：重算正文与原件对不上
        conn = core.connect(db)
        try:
            conn.execute(
                "UPDATE decision SET text = '被篡改的保留文字' WHERE status = 'keep'"
            )
        finally:
            conn.close()

        response = client.post("/editions/1/repair", follow_redirects=True)
        assert "核对不符" in response.data.decode()
        assert client.get("/editions/1/repaired").status_code == 404

    def test_repair_fails_when_count_mismatches(self, app_db):
        application, db = app_db
        client = application.test_client()
        make_legacy_edition(client, db)
        # 把校订改成删除：重算条数少于原件
        conn = core.connect(db)
        try:
            conn.execute(
                "UPDATE decision SET status = 'delete', text = '' "
                "WHERE status = 'keep'"
            )
        finally:
            conn.close()

        response = client.post("/editions/1/repair", follow_redirects=True)
        assert "条数不同" in response.data.decode()
        assert client.get("/editions/1/repaired").status_code == 404
