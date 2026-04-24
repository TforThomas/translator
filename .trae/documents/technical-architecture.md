## 1. 架构设计
```mermaid
graph TD
    subgraph Frontend ["React Web App"]
        A["主工作台 (上传/监控)"]
        B["术语管理 (Context Pack)"]
        C["结果下载 (EPUB)"]
    end
    subgraph Backend ["FastAPI / Python"]
        D["API Gateway / Auth"]
        E["文件解析器 (PDF/EPUB)"]
        F["翻译编排器 (草译/润色/质检)"]
    end
    subgraph Task Queue ["Celery + Redis"]
        G["任务调度 (Task Dispatch)"]
        H["Worker (翻译段落)"]
        I["Worker (OCR处理)"]
    end
    subgraph Data Layer ["PostgreSQL"]
        J[("Users/Projects")]
        K[("Context/Terminologies")]
        L[("Segments/Translations")]
    end
    subgraph External Services ["Cloud APIs"]
        M["云端 LLM API (OpenAI/Claude)"]
        N["云端 OCR API (可选)"]
    end

    A -->|HTTP/REST| D
    B -->|HTTP/REST| D
    C -->|HTTP/REST| D
    D --> E
    E --> G
    F --> M
    I --> N
    G --> H
    H --> F
    H -->|读写进度| L
    H -->|读取术语| K
    D -->|状态查询| L
```

## 2. 技术栈说明
- **前端 (Frontend)**: React@18 + TailwindCSS@3 + Vite
- **后端 (Backend)**: Python 3.10+ / FastAPI
- **任务队列**: Celery (通过 Redis 作为 Broker/Backend)
- **数据库 (DB)**: PostgreSQL (通过 SQLAlchemy ORM 交互)
- **文件存储**: 本地挂载卷 (Volumes) 或 MinIO
- **核心依赖**: `ebooklib` (解析EPUB), `pdfplumber` / `PyMuPDF` (解析PDF), `langchain` / 原生 HTTP requests (调用大模型API)

## 3. 路由定义
| 路由 | 用途 |
|-------|---------|
| `/api/projects` | GET 获取项目列表，POST 创建翻译项目 |
| `/api/projects/{id}/upload` | POST 上传 EPUB/PDF 并触发解析任务 |
| `/api/projects/{id}/status` | GET 获取项目切分、翻译与合并进度 |
| `/api/projects/{id}/terms` | GET 获取已抽取的术语候选，POST 保存/修改术语配置 |
| `/api/projects/{id}/download` | GET 下载生成的单语译文 EPUB 文件 |
| `/api/tasks/retry` | POST 手动重试失败的段落翻译任务 (断点续跑) |

## 4. API 定义 (示例)
### 4.1 创建翻译项目
**POST `/api/projects`**
请求体 (Request):
```json
{
  "name": "三体(英文版翻译)",
  "source_lang": "en",
  "target_lang": "zh",
  "enable_ocr": false
}
```
响应体 (Response):
```json
{
  "id": "uuid-1234",
  "name": "三体(英文版翻译)",
  "status": "created",
  "created_at": "2026-04-16T10:00:00Z"
}
```

## 5. 服务器端架构图
```mermaid
flowchart TD
    Controller["FastAPI Routers"] --> Service["Project & Translation Service"]
    Service --> Parser["Document Parser (EPUB/PDF)"]
    Service --> CeleryQueue["Celery Task Producer"]
    CeleryQueue --> Worker["Celery Worker (LLM Call)"]
    Worker --> QA["Quality Assurance Checker"]
    Worker --> Repository["SQLAlchemy Repository"]
    Repository --> Database[("PostgreSQL")]
```

## 6. 数据模型设计
### 6.1 数据模型定义 (ER Diagram)
```mermaid
erDiagram
    PROJECT ||--o{ CHAPTER : contains
    PROJECT ||--o{ TERMINOLOGY : owns
    CHAPTER ||--o{ SEGMENT : contains
    PROJECT {
        uuid id PK
        string name
        string source_file_path
        string status "uploading, parsing, pending_terms, translating, completed"
        boolean enable_ocr
        datetime created_at
    }
    CHAPTER {
        uuid id PK
        uuid project_id FK
        integer order_index
        string title
        string status
    }
    SEGMENT {
        uuid id PK
        uuid chapter_id FK
        integer order_index
        text original_text
        text translated_text
        string status "pending, translating, qa_failed, completed"
        integer retry_count
    }
    TERMINOLOGY {
        uuid id PK
        uuid project_id FK
        string original_term
        string translated_term
        string type "character, place, tech"
        boolean is_confirmed
    }
```

### 6.2 DDL 示例
```sql
CREATE TABLE projects (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    source_file_path TEXT,
    status VARCHAR(50) DEFAULT 'created',
    enable_ocr BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE terminologies (
    id UUID PRIMARY KEY,
    project_id UUID REFERENCES projects(id),
    original_term VARCHAR(255) NOT NULL,
    translated_term VARCHAR(255),
    type VARCHAR(50),
    is_confirmed BOOLEAN DEFAULT FALSE
);

CREATE TABLE segments (
    id UUID PRIMARY KEY,
    chapter_id UUID, -- References Chapters
    order_index INT,
    original_text TEXT NOT NULL,
    translated_text TEXT,
    status VARCHAR(50) DEFAULT 'pending',
    retry_count INT DEFAULT 0
);
```
