import time
from pathlib import Path

import fitz
import httpx

from backend.services.parser import _pdf_text_to_segments, PDF_SEGMENT_MAX_CHARS, PDF_SEGMENT_MIN_CHARS

BASE = "http://127.0.0.1:8000"
PDF_PATH = Path("bench_wrapped.pdf")


def build_pdf(path: Path, pages: int = 12):
    doc = fitz.open()
    text = (
        "In this paper we study the dynamic behavior of large language models under domain shift. "
        "The experimental results indicate stable convergence and better robustness with retrieval augmentation. "
        "We further analyze latency, memory footprint, and failure recovery in production pipelines."
    )
    words = text.split(" ")
    wrapped_lines = []
    for i in range(0, len(words), 2):
        wrapped_lines.append(" ".join(words[i:i + 2]))

    for _ in range(pages):
        page = doc.new_page()
        y = 60
        for _ in range(18):
            for line in wrapped_lines:
                page.insert_text((50, y), line, fontsize=10)
                y += 12
                if y > 780:
                    break
            if y > 780:
                break
            y += 8
    doc.save(path.as_posix())
    doc.close()


def estimate_segments(path: Path):
    doc = fitz.open(path.as_posix())
    line_count = 0
    seg_count = 0
    for page in doc:
        raw = page.get_text()
        line_count += len([ln for ln in raw.splitlines() if ln.strip()])
        seg_count += len(_pdf_text_to_segments(raw, PDF_SEGMENT_MIN_CHARS, PDF_SEGMENT_MAX_CHARS))
    doc.close()
    return line_count, seg_count


def main():
    build_pdf(PDF_PATH)
    line_count, seg_count = estimate_segments(PDF_PATH)
    print(f"[probe] non-empty lines={line_count}, parsed segments={seg_count}")

    with httpx.Client(timeout=60) as client:
        project = client.post(
            f"{BASE}/api/projects",
            json={"name": "speed-probe", "source_lang": "en", "target_lang": "zh", "enable_ocr": False},
        ).json()
        project_id = project["id"]
        print(f"[probe] project={project_id}")

        with open(PDF_PATH, "rb") as f:
            files = {"file": (PDF_PATH.name, f, "application/pdf")}
            up = client.post(f"{BASE}/api/projects/{project_id}/upload", files=files)
            up.raise_for_status()

        # wait parse
        for _ in range(120):
            status = client.get(f"{BASE}/api/projects/{project_id}/status").json()
            if status.get("status") == "pending_terms":
                break
            time.sleep(1)

        st = client.get(f"{BASE}/api/projects/{project_id}/status").json()
        print(f"[probe] parse status={st.get('status')}, summary={st.get('segment_summary')}")

        client.post(f"{BASE}/api/projects/{project_id}/terms/confirm_all").raise_for_status()

        # observe throughput for 60s
        start = time.time()
        begin_completed = 0
        last_completed = 0
        for i in range(60):
            cur = client.get(f"{BASE}/api/projects/{project_id}/status").json()
            summary = cur.get("segment_summary", {})
            completed = summary.get("completed", 0)
            if i == 0:
                begin_completed = completed
            last_completed = completed
            if i % 10 == 0:
                print(f"[probe] t={i:02d}s status={cur.get('status')} progress={cur.get('progress')} completed={completed}")
            if cur.get("status") in {"completed", "failed"}:
                break
            time.sleep(1)

        elapsed = max(1e-6, time.time() - start)
        delta = max(0, last_completed - begin_completed)
        rate = delta / elapsed * 60
        print(f"[probe] completed delta={delta} in {elapsed:.1f}s, est throughput={rate:.2f} seg/min")


if __name__ == "__main__":
    main()
