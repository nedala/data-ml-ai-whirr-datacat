"""Files (/files/): path safety (traversal, symlink escapes, protected
folders) and the browser API (upload, list, preview, download, rename,
delete). No services needed."""
from tests import _env  # noqa: F401  (must be first: sets env)
from tests._env import H, TMP

import io
import os
import unittest

from fastapi.testclient import TestClient

import app as wbapp
from wb import config, fsroots
from wb.fsroots import PathError

LAKE = TMP / "lake"
WORK = TMP / "work"
OUTSIDE = TMP / "outside"
OUTSIDE.mkdir(exist_ok=True)
(OUTSIDE / "secret.txt").write_text("top secret")


class PathSafety(unittest.TestCase):
    def test_clean_rel(self):
        self.assertEqual(fsroots.clean_rel("/landing//a/./b.csv"), "landing/a/b.csv")
        self.assertEqual(fsroots.clean_rel(""), "")
        for bad in ("../x", "landing/../../etc", "a/..", "..\\x", "a\x00b"):
            with self.assertRaises(PathError, msg=bad):
                fsroots.clean_rel(bad)

    def test_absolute_paths_stay_inside_the_root(self):
        with self.assertRaises(PathError) as cm:
            fsroots.resolve("lake", "/etc/passwd")
        self.assertEqual(cm.exception.status, 404)  # i.e. <lake>/etc/passwd, which doesn't exist

    def test_unknown_root(self):
        with self.assertRaises(PathError):
            fsroots.resolve("etc", "")

    def test_symlink_escape_is_refused(self):
        link = LAKE / "landing" / "escape"
        if not link.is_symlink():
            os.symlink(OUTSIDE, link)
        with self.assertRaises(PathError) as cm:
            fsroots.resolve("lake", "landing/escape/secret.txt")
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(PathError):
            fsroots.resolve("lake", "landing/escape")
        names = [e["name"] for e in fsroots.listing("lake", "landing")["entries"]]
        self.assertNotIn("escape", names)

    def test_root_symlink_to_outside_is_refused(self):
        link = WORK / "sneaky.txt"
        if not link.is_symlink():
            os.symlink(OUTSIDE / "secret.txt", link)
        with self.assertRaises(PathError):
            fsroots.resolve("work", "sneaky.txt")
        # deleting the link removes the link only, never its target
        fsroots.delete("work", ["sneaky.txt"])
        self.assertFalse(link.is_symlink())
        self.assertTrue((OUTSIDE / "secret.txt").exists())

    def test_internal_symlink_is_allowed(self):
        (LAKE / "reference" / "rates.csv").write_text("a,b\n1,2\n")
        link = LAKE / "landing" / "rates-link.csv"
        if not link.is_symlink():
            os.symlink(LAKE / "reference" / "rates.csv", link)
        self.assertTrue(fsroots.resolve("lake", "landing/rates-link.csv").is_file())

    def test_protected_folders(self):
        for rel in ("", "landing", "exports"):
            with self.assertRaises(PathError):
                fsroots.delete("lake", [rel])
        with self.assertRaises(PathError):
            fsroots.rename("lake", "landing", "x")
        with self.assertRaises(PathError):
            fsroots.delete("work", [""])

    def test_names(self):
        for bad in ("", ".", "..", "a/b", "a\\b", "x\ny", "z" * 300):
            with self.assertRaises(PathError, msg=repr(bad)):
                fsroots.check_name(bad)
        self.assertEqual(fsroots.check_name(" ok name.csv "), "ok name.csv")

    def test_rename_cannot_move_out_of_folder(self):
        (WORK / "r.txt").write_text("x")
        with self.assertRaises(PathError):
            fsroots.rename("work", "r.txt", "../r.txt")
        self.assertEqual(fsroots.rename("work", "r.txt", "s.txt"), "s.txt")
        self.assertTrue((WORK / "s.txt").exists())

    def test_safe_filename(self):
        self.assertEqual(fsroots.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(fsroots.safe_filename("a<b>:c?.csv"), "a_b__c_.csv")
        self.assertEqual(fsroots.safe_filename(""), "download")


class FilesApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(wbapp.app)

    def up(self, path, name, data, **kw):
        return self.c.put("/files/api/upload", params={"root": "lake", "path": path, "name": name, **kw},
                          content=data, headers=H)

    def test_csrf_header_required(self):
        r = self.c.post("/files/api/mkdir", json={"root": "work", "path": "", "name": "x"})
        self.assertEqual(r.status_code, 403)

    def test_status(self):
        s = self.c.get("/files/api/status").json()
        self.assertEqual([r["id"] for r in s["roots"]], ["lake", "work"])
        self.assertEqual(s["roots"][0]["folders"], list(config.LAKE_FOLDERS))

    def test_upload_list_preview_download_rename_delete(self):
        csv = b"Policy No,Premium,Inception\nP1,100.5,2024-01-01\nP2,200,2024-02-01\n"
        r = self.up("landing", "policies.csv", csv)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["path"], "landing/policies.csv")
        self.assertEqual(self.up("landing", "policies.csv", csv).status_code, 409)
        self.assertEqual(self.up("landing", "policies.csv", csv, overwrite=1).status_code, 200)
        self.assertEqual(sorted(p.name for p in (LAKE / "landing").glob(".upload-*")), [])

        ls = self.c.get("/files/api/list", params={"root": "lake", "path": "landing"}).json()
        e = next(x for x in ls["entries"] if x["name"] == "policies.csv")
        self.assertEqual((e["kind"], e["size"]), ("table", len(csv)))
        self.assertEqual([c["name"] for c in ls["crumbs"]], ["Shared lake", "landing"])

        p = self.c.get("/files/api/preview", params={"root": "lake", "path": "landing/policies.csv"}).json()
        self.assertEqual(p["type"], "table")
        self.assertEqual([c["type"] for c in p["columns"]], ["text", "number", "date"])
        self.assertEqual(p["total_rows"], 2)

        d = self.c.get("/files/api/download", params={"root": "lake", "path": "landing/policies.csv"})
        self.assertEqual(d.content, csv)
        self.assertIn("attachment", d.headers["content-disposition"])

        r = self.c.post("/files/api/rename", json={"root": "lake", "path": "landing/policies.csv", "new_name": "pol.csv"}, headers=H)
        self.assertEqual(r.json()["path"], "landing/pol.csv")
        r = self.c.post("/files/api/delete", json={"root": "lake", "paths": ["landing/pol.csv"]}, headers=H)
        self.assertEqual(r.json()["deleted"], ["landing/pol.csv"])
        self.assertFalse((LAKE / "landing" / "pol.csv").exists())

    def test_traversal_via_api(self):
        for params in ({"root": "lake", "path": "../../etc"}, {"root": "lake", "path": "landing/../../"}):
            self.assertEqual(self.c.get("/files/api/list", params=params).status_code, 400)
        r = self.c.get("/files/api/download", params={"root": "work", "path": "../outside/secret.txt"})
        self.assertEqual(r.status_code, 400)
        r = self.c.put("/files/api/upload", params={"root": "lake", "path": "landing", "name": "../x.csv"}, content=b"x", headers=H)
        self.assertEqual(r.status_code, 400)
        r = self.c.put("/files/api/upload", params={"root": "lake", "path": "../..", "name": "x.csv"}, content=b"x", headers=H)
        self.assertEqual(r.status_code, 400)

    def test_upload_size_limit(self):
        old = config.UPLOAD_MAX_BYTES
        config.UPLOAD_MAX_BYTES = 10
        try:
            r = self.up("landing", "big.bin", b"x" * 100)
            self.assertEqual(r.status_code, 413)
            self.assertFalse((LAKE / "landing" / "big.bin").exists())
            self.assertEqual(list((LAKE / "landing").glob(".upload-*")), [])
        finally:
            config.UPLOAD_MAX_BYTES = old

    def test_mkdir(self):
        r = self.c.post("/files/api/mkdir", json={"root": "work", "path": "", "name": "analysis"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertTrue((WORK / "analysis").is_dir())
        r = self.c.post("/files/api/mkdir", json={"root": "work", "path": "", "name": "analysis"}, headers=H)
        self.assertEqual(r.status_code, 409)

    def test_preview_text_image_excel_parquet(self):
        (WORK / "notes.md").write_text("# Hello\n" + "x" * 10)
        p = self.c.get("/files/api/preview", params={"root": "work", "path": "notes.md"}).json()
        self.assertEqual(p["type"], "text")
        self.assertTrue(p["text"].startswith("# Hello"))

        (WORK / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        r = self.c.get("/files/api/raw", params={"root": "work", "path": "pic.svg"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("sandbox", r.headers["content-security-policy"])
        self.assertEqual(self.c.get("/files/api/raw", params={"root": "work", "path": "notes.md"}).status_code, 400)

        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Losses"
        ws.append(["Claim", "Paid"])
        ws.append(["C1", 10.0])
        wb.create_sheet("Other").append(["a"])
        buf = io.BytesIO()
        wb.save(buf)
        (WORK / "losses.xlsx").write_bytes(buf.getvalue())
        p = self.c.get("/files/api/preview", params={"root": "work", "path": "losses.xlsx"}).json()
        self.assertEqual((p["type"], p["sheets"], p["sheet"]), ("table", ["Losses", "Other"], "Losses"))
        self.assertEqual(p["rows"], [["C1", 10.0]])
        p = self.c.get("/files/api/preview", params={"root": "work", "path": "losses.xlsx", "sheet": "Other"}).json()
        self.assertEqual(p["sheet"], "Other")

        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.table({"id": [1, 2, 3], "name": ["a", "b", None]}), WORK / "t.parquet")
        p = self.c.get("/files/api/preview", params={"root": "work", "path": "t.parquet"}).json()
        self.assertEqual((p["total_rows"], [c["type"] for c in p["columns"]]), (3, ["integer", "text"]))

    def test_pages_render(self):
        for page in ("/files/", "/ingress/", "/pkg/"):
            r = self.c.get(page)
            self.assertEqual(r.status_code, 200, page)
            self.assertIn("<div id=\"app\">", r.text)


if __name__ == "__main__":
    unittest.main()
