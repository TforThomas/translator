import time
import json
import httpx
from pathlib import Path

BASE = "http://127.0.0.1:8000"


def wait_status(client: httpx.Client, project_id: str, timeout: int = 120):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        data = client.get(f"{BASE}/api/projects/{project_id}/status", timeout=30).json()
        status = data.get("status")
        if status != last:
            print(f"  status => {status}, progress={data.get('progress')}")
            last = status
        if status in {"pending_terms", "translating", "completed", "failed"}:
            return data
        time.sleep(1)
    raise TimeoutError(f"project {project_id} status timeout")


def create_and_upload(client: httpx.Client, name: str, file_path: str, enable_ocr: bool):
    payload = {
        "name": name,
        "source_lang": "en",
        "target_lang": "zh",
        "enable_ocr": enable_ocr,
    }
    project = client.post(f"{BASE}/api/projects", json=payload, timeout=30).json()
    project_id = project["id"]
    print(f"[{name}] created: {project_id}")

    with open(file_path, "rb") as f:
        files = {"file": (Path(file_path).name, f)}
        upload = client.post(f"{BASE}/api/projects/{project_id}/upload", files=files, timeout=120)
        upload.raise_for_status()
    print(f"[{name}] upload ok")

    status_data = wait_status(client, project_id)
    terms = client.get(f"{BASE}/api/projects/{project_id}/terms", timeout=30)
    terms.raise_for_status()
    print(f"[{name}] terms count: {len(terms.json())}")

    if status_data.get("status") == "pending_terms":
        r = client.post(f"{BASE}/api/projects/{project_id}/terms/confirm_all", timeout=30)
        r.raise_for_status()
        print(f"[{name}] confirm_all triggered")

        # 观察翻译是否启动（短等待）
        obs_start = time.time()
        while time.time() - obs_start < 30:
            data = client.get(f"{BASE}/api/projects/{project_id}/status", timeout=30).json()
            if data.get("status") in {"translating", "completed", "failed"}:
                print(f"[{name}] translation entered: {data.get('status')} (progress={data.get('progress')})")
                break
            time.sleep(1)

    retry_res = client.post(f"{BASE}/api/tasks/retry", json={"project_id": project_id}, timeout=30)
    retry_res.raise_for_status()
    print(f"[{name}] retry endpoint ok: {retry_res.json()}")

    return project_id


def main():
    if not Path("test.epub").exists() or not Path("test.pdf").exists():
        raise FileNotFoundError("test.epub or test.pdf not found")

    with httpx.Client(timeout=60) as client:
        health = client.get(f"{BASE}/api/health").json()
        print("health:", health)

        settings = client.get(f"{BASE}/api/settings").json()
        print("settings model:", settings.get("model_name"), "base_url:", settings.get("openai_base_url"))
        print("api key configured:", bool(settings.get("openai_api_key")))

        epub_id = create_and_upload(client, "acceptance-epub", "test.epub", enable_ocr=False)
        pdf_id = create_and_upload(client, "acceptance-pdf", "test.pdf", enable_ocr=True)

        print("\nSUMMARY")
        print(json.dumps({"epub_project_id": epub_id, "pdf_project_id": pdf_id}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
