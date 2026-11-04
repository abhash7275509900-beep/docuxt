import os
"""
Regression test — Phase 6 (documents/versions, sharing, comments, locking).
"""
import sys
import io
import requests

BASE = os.environ.get("DOCENGINE_BASE_URL", "http://localhost:8000")
results = []


def check(desc, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, desc, detail))
    print(f"[{status}] {desc}" + (f"  -- {detail}" if detail and status == 'FAIL' else ""))
    return condition


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def upload(token, filename, content_bytes):
    return requests.post(f"{BASE}/files", headers=auth_header(token),
                          files={"file": (filename, io.BytesIO(content_bytes))})


def make_pdf_bytes(text_lines):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 14)
    y = 700
    for line in text_lines:
        c.drawString(72, y, line)
        y -= 30
    c.save()
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "p6owner@test.com", "password": "testpass12"})
token_owner = signup.json()["token"] if signup.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "p6owner@test.com", "password": "testpass12"}).json()["token"]

signup2 = requests.post(f"{BASE}/auth/signup",
                         json={"email": "p6other@test.com", "password": "testpass12"})
token_other = signup2.json()["token"] if signup2.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "p6other@test.com", "password": "testpass12"}).json()["token"]

check("both test users authenticated", bool(token_owner) and bool(token_other))


# ============================================================
print("=" * 60)
print("Documents & version history")
print("=" * 60)

fid_v1 = upload(token_owner, "v1.pdf", make_pdf_bytes(["Version 1 content"])).json()["file_id"]
r = requests.post(f"{BASE}/documents", headers=auth_header(token_owner),
                   json={"file_id": fid_v1, "title": "Test Doc"})
check("create document succeeds", r.status_code == 200, r.text)
doc_id = r.json().get("document_id")

doc_info = requests.get(f"{BASE}/documents/{doc_id}", headers=auth_header(token_owner)).json()
check("document starts at version 1", doc_info.get("document", {}).get("current_version") == 1, str(doc_info))
check("current version points at the right file", doc_info.get("current_version", {}).get("file_id") == fid_v1)

other_get = requests.get(f"{BASE}/documents/{doc_id}", headers=auth_header(token_other))
check("other user cannot view the document (403)", other_get.status_code == 403)

fid_v2 = upload(token_owner, "v2.pdf", make_pdf_bytes(["Version 2 content - edited"])).json()["file_id"]
r = requests.post(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(token_owner),
                   json={"file_id": fid_v2, "label": "second draft"})
check("add version succeeds", r.status_code == 200, r.text)
check("new version is numbered 2", r.json().get("version_number") == 2, r.text)

fid_v3 = upload(token_owner, "v3.pdf", make_pdf_bytes(["Version 3 content - final"])).json()["file_id"]
r = requests.post(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(token_owner),
                   json={"file_id": fid_v3, "label": "final"})
check("third version numbered 3", r.json().get("version_number") == 3, r.text)

doc_info2 = requests.get(f"{BASE}/documents/{doc_id}", headers=auth_header(token_owner)).json()
check("document current_version now 3", doc_info2["document"]["current_version"] == 3)
check("current version file_id is v3", doc_info2["current_version"]["file_id"] == fid_v3)

versions = requests.get(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(token_owner)).json()
check("version list has all 3 versions", len(versions.get("versions", [])) == 3, str(versions))

# revert to version 1 - should create version 4, not delete 2/3
r = requests.post(f"{BASE}/documents/{doc_id}/revert", headers=auth_header(token_owner),
                   json={"version_number": 1})
check("revert succeeds", r.status_code == 200, r.text)
check("revert creates version 4 (not a destructive rollback)", r.json().get("version_number") == 4, r.text)
check("reverted version points back at v1's file_id", r.json().get("file_id") == fid_v1, r.text)

versions_after = requests.get(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(token_owner)).json()
check("all 4 versions still present after revert (history preserved)",
      len(versions_after.get("versions", [])) == 4, str(versions_after))

other_add_version = requests.post(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(token_other),
                                   json={"file_id": fid_v2})
check("other user cannot add a version to someone else's document (403)",
      other_add_version.status_code == 403)


# ============================================================
print("=" * 60)
print("Document collaborators — genuine multi-user access")
print("=" * 60)

before_collab = requests.get(f"{BASE}/documents/{doc_id}", headers=auth_header(token_other))
check("non-collaborator denied document access (403)", before_collab.status_code == 403)

add_collab = requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(token_owner),
                            json={"email": "p6other@test.com", "permission": "edit"})
check("owner can add a collaborator with edit permission", add_collab.status_code == 200, add_collab.text)

after_collab = requests.get(f"{BASE}/documents/{doc_id}", headers=auth_header(token_other))
check("collaborator can now view the document", after_collab.status_code == 200)

collab_add_version = requests.post(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(token_other),
                                    json={"file_id": upload(token_other, "cv.pdf",
                                          make_pdf_bytes(["Collaborator's edit"])).json()["file_id"]})
check("edit-permission collaborator can add a version", collab_add_version.status_code == 200,
      collab_add_version.text)

collab_list = requests.get(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(token_owner))
check("collaborator list shows the added collaborator",
      any(c["email"] == "p6other@test.com" for c in collab_list.json().get("collaborators", [])),
      collab_list.text)

not_owner_manage = requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(token_other),
                                  json={"email": "p6owner@test.com", "permission": "edit"})
check("collaborator (non-owner) cannot manage collaborators (403)", not_owner_manage.status_code == 403)

other_user_id = requests.get(f"{BASE}/auth/me", headers=auth_header(token_other)).json()["user_id"]
remove_collab = requests.delete(f"{BASE}/documents/{doc_id}/collaborators/{other_user_id}",
                                 headers=auth_header(token_owner))
check("owner can remove a collaborator", remove_collab.status_code == 200, remove_collab.text)

after_removal = requests.get(f"{BASE}/documents/{doc_id}", headers=auth_header(token_other))
check("removed collaborator loses access (403)", after_removal.status_code == 403)


# ============================================================
print("=" * 60)
print("Real lock contention between two different users")
print("=" * 60)

# re-add as collaborator for this test
requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(token_owner),
              json={"email": "p6other@test.com", "permission": "edit"})

lock_owner = requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(token_owner))
check("owner acquires the lock first", lock_owner.status_code == 200, lock_owner.text)

lock_other = requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(token_other))
check("a genuinely different user is blocked (409, not 403 — they DO have access, it's just locked)",
      lock_other.status_code == 409, lock_other.text)

requests.post(f"{BASE}/documents/{doc_id}/unlock", headers=auth_header(token_owner))
lock_other2 = requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(token_other))
check("other user can acquire the lock once it's released", lock_other2.status_code == 200,
      lock_other2.text)
requests.post(f"{BASE}/documents/{doc_id}/unlock", headers=auth_header(token_other))


# ============================================================
print("=" * 60)
print("Edit locking (pessimistic — not real-time CRDT)")
print("=" * 60)

r = requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(token_owner))
check("owner acquires lock", r.status_code == 200, r.text)

r2 = requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(token_owner))
check("owner re-acquiring their own lock is a no-op success", r2.status_code == 200, r2.text)

r3 = requests.post(f"{BASE}/documents/{doc_id}/unlock", headers=auth_header(token_owner))
check("owner releases lock", r3.status_code == 200, r3.text)

r4 = requests.post(f"{BASE}/documents/{doc_id}/unlock", headers=auth_header(token_owner))
check("releasing an already-unlocked document fails cleanly (409)", r4.status_code == 409)


# ============================================================
print("=" * 60)
print("Sharing")
print("=" * 60)

fid_share = upload(token_owner, "share_me.pdf", make_pdf_bytes(["Shared document content XKCD5566"])).json()["file_id"]

r = requests.post(f"{BASE}/share", headers=auth_header(token_owner),
                   json={"file_id": fid_share, "permission": "view"})
check("create view-only share link succeeds", r.status_code == 200, r.text)
view_token = r.json().get("token")

# public access, no auth header at all
info = requests.get(f"{BASE}/share/{view_token}")
check("public share info endpoint works with no auth", info.status_code == 200, info.text)
check("share info reports correct permission", info.json().get("permission") == "view")

dl = requests.get(f"{BASE}/share/{view_token}/download")
check("public download via view share succeeds", dl.status_code == 200)
import pdfplumber
with pdfplumber.open(io.BytesIO(dl.content)) as pdf:
    text = pdf.pages[0].extract_text() or ""
check("downloaded content via share is correct", "XKCD5566" in text, repr(text))

# view-only link should NOT allow commenting
comment_attempt = requests.post(f"{BASE}/share/{view_token}/comments",
                                 json={"author_name": "Reviewer", "text": "nice doc"})
check("view-only share link rejects comment attempts (403)", comment_attempt.status_code == 403,
      comment_attempt.text)

# comment-permission link
r = requests.post(f"{BASE}/share", headers=auth_header(token_owner),
                   json={"file_id": fid_share, "permission": "comment"})
comment_token = r.json().get("token")

comment_ok = requests.post(f"{BASE}/share/{comment_token}/comments",
                            json={"author_name": "External Reviewer",
                                  "text": "Looks good, one typo on page 1",
                                  "mentions": ["alice@company.com"]})
check("comment-permission share link allows posting a comment", comment_ok.status_code == 200,
      comment_ok.text)

fake_token_check = requests.get(f"{BASE}/share/nonexistenttoken12345")
check("nonexistent share token returns 404", fake_token_check.status_code == 404)

revoke = requests.delete(f"{BASE}/share/{view_token}", headers=auth_header(token_owner))
check("owner can revoke a share link", revoke.status_code == 200, revoke.text)

after_revoke = requests.get(f"{BASE}/share/{view_token}")
check("revoked share link no longer accessible (403)", after_revoke.status_code == 403,
      after_revoke.text)

other_revoke = requests.delete(f"{BASE}/share/{comment_token}", headers=auth_header(token_other))
check("non-owner cannot revoke someone else's share link (404)", other_revoke.status_code == 404)

shares_list = requests.get(f"{BASE}/files/{fid_share}/shares", headers=auth_header(token_owner))
check("owner can list share links for their file", shares_list.status_code == 200 and
      len(shares_list.json().get("shares", [])) == 2, shares_list.text)

# expiry: create a share that's already expired (negative hours) and confirm it's rejected
import time as _t6_time
r = requests.post(f"{BASE}/share", headers=auth_header(token_owner),
                   json={"file_id": fid_share, "permission": "view", "expires_in_hours": 0.00005})
expired_token = r.json().get("token")
_t6_time.sleep(0.25)
expired_check = requests.get(f"{BASE}/share/{expired_token}")
check("already-expired share link is rejected (403)", expired_check.status_code == 403,
      expired_check.text)


# ============================================================
print("=" * 60)
print("Comments")
print("=" * 60)

fid_comment_doc = upload(token_owner, "commentable.pdf",
                          make_pdf_bytes(["Doc for comment testing"])).json()["file_id"]

r = requests.post(f"{BASE}/comments", headers=auth_header(token_owner),
                   json={"file_id": fid_comment_doc, "text": "Owner's own comment",
                         "page": 1, "mentions": ["bob@company.com"]})
check("owner can comment on their own file", r.status_code == 200, r.text)
owner_comment_id = r.json().get("comment_id")

listing = requests.get(f"{BASE}/comments/{fid_comment_doc}", headers=auth_header(token_owner))
check("comment list returns the comment with mentions intact",
      len(listing.json().get("comments", [])) == 1 and
      listing.json()["comments"][0]["mentions"] == ["bob@company.com"], listing.text)

other_list = requests.get(f"{BASE}/comments/{fid_comment_doc}", headers=auth_header(token_other))
check("other user cannot list comments on a file they don't own (403)", other_list.status_code == 403)

resolve = requests.post(f"{BASE}/comments/{owner_comment_id}/resolve", headers=auth_header(token_owner))
check("resolving a comment succeeds", resolve.status_code == 200, resolve.text)
listing2 = requests.get(f"{BASE}/comments/{fid_comment_doc}", headers=auth_header(token_owner))
check("resolved comment shows resolved=1", listing2.json()["comments"][0]["resolved"] == 1)

other_delete = requests.delete(f"{BASE}/comments/{owner_comment_id}", headers=auth_header(token_other))
check("other user cannot delete someone else's comment (404)", other_delete.status_code == 404)

own_delete = requests.delete(f"{BASE}/comments/{owner_comment_id}", headers=auth_header(token_owner))
check("author can delete their own comment", own_delete.status_code == 200, own_delete.text)

listing3 = requests.get(f"{BASE}/comments/{fid_comment_doc}", headers=auth_header(token_owner))
check("comment count is 0 after delete", len(listing3.json()["comments"]) == 0)

# anonymous comment via share link should show up in owner's authenticated comment list too
r = requests.post(f"{BASE}/share", headers=auth_header(token_owner),
                   json={"file_id": fid_comment_doc, "permission": "comment"})
anon_token = r.json()["token"]
requests.post(f"{BASE}/share/{anon_token}/comments",
              json={"author_name": "Guest Reviewer", "text": "Anonymous feedback here"})
listing4 = requests.get(f"{BASE}/comments/{fid_comment_doc}", headers=auth_header(token_owner))
comments4 = listing4.json().get("comments", [])
check("anonymous share-link comment appears in owner's comment list",
      any(c["author_label"] == "Guest Reviewer" and c["author_id"] is None for c in comments4),
      str(comments4))

anon_own_list = requests.get(f"{BASE}/share/{anon_token}/comments")
check("comments also visible via the share link itself (no auth)", anon_own_list.status_code == 200 and
      len(anon_own_list.json().get("comments", [])) >= 1, anon_own_list.text)


# ============================================================
print("=" * 60)
print("SUMMARY")
print("=" * 60)
passed = sum(1 for s, _, _ in results if s == "PASS")
failed = [(d, det) for s, d, det in results if s == "FAIL"]
print(f"{passed}/{len(results)} checks passed")
if failed:
    print("FAILED CHECKS:")
    for d, det in failed:
        print(f"  - {d}" + (f"  [{det}]" if det else ""))
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
