import time
import httpx

BASE = "http://127.0.0.1:8000"
FILE = "test.pdf"

with httpx.Client(timeout=60) as client:
    created = client.post(
        f"{BASE}/api/projects",
        json={"name": "pause-probe", "source_lang": "en", "target_lang": "zh", "enable_ocr": False},
    ).json()
    pid = created["id"]
    print("project", pid)

    with open(FILE, "rb") as f:
        client.post(f"{BASE}/api/projects/{pid}/upload", files={"file": (FILE, f, "application/pdf")}).raise_for_status()

    for _ in range(60):
        st = client.get(f"{BASE}/api/projects/{pid}/status").json()
        if st["status"] == "pending_terms":
            break
        time.sleep(1)

    client.post(f"{BASE}/api/projects/{pid}/terms/confirm_all").raise_for_status()
    time.sleep(2)

    paused = client.post(f"{BASE}/api/tasks/pause", json={"project_id": pid}).json()
    print("pause:", paused)

    time.sleep(3)
    st1 = client.get(f"{BASE}/api/projects/{pid}/status").json()
    print("after pause status:", st1["status"], "progress:", st1["progress"], "summary:", st1["segment_summary"])

    resumed = client.post(f"{BASE}/api/tasks/resume", json={"project_id": pid}).json()
    print("resume:", resumed)

    for _ in range(90):
        st2 = client.get(f"{BASE}/api/projects/{pid}/status").json()
        if st2["status"] in {"completed", "failed", "paused"}:
            print("final:", st2["status"], st2["progress"], st2["segment_summary"])
            if st2["status"] == "completed":
                dl = client.get(f"{BASE}/api/projects/{pid}/download")
                print("download:", dl.status_code, len(dl.content))
            break
        time.sleep(1)
