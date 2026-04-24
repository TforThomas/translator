export type ProjectStatus =
  | "created"
  | "uploading"
  | "parsing"
  | "pending_terms"
  | "translating"
  | "paused"
  | "completed"
  | "failed";

export interface SegmentSummary {
  pending: number;
  drafting: number;
  polishing: number;
  completed: number;
  qa_failed: number;
  failed: number;
}

export interface ProjectSummary {
  id: string;
  name: string;
  status: ProjectStatus;
  progress: number;
  created_at: string;
}

export interface ChapterStatus {
  id: string;
  title: string;
  status: "pending" | "translating" | "completed" | "failed";
  progress: number;
  segment_summary: SegmentSummary;
}

export interface ProjectDetail {
  id: string;
  name: string;
  progress: number;
  status: ProjectStatus;
  chapters: ChapterStatus[];
  segment_summary: SegmentSummary;
  quality_summary?: {
    translated_segments: number;
    auto_repaired_segments: number;
    qa_failed_segments: number;
    high_english_residue_segments: number;
    too_short_translation_segments: number;
    avg_retry_count: number;
  };
}

export interface RetryTasksResponse {
  ok: boolean;
  retried: number;
}

export interface TaskControlResponse {
  ok: boolean;
  status: ProjectStatus;
}

export interface TerminologyItem {
  id: string;
  original_term: string;
  translated_term: string;
  type: string;
  is_confirmed: boolean;
}
