"""FastAPI 网页与 API。

- 网页：项目列表/创建、按项目上传 ZIP、显示包状态与拒收原因、下载处理包。
- JSON API：供脚本（如跨批引用示例）与双实例并发演示使用。
- 处理在后台线程池执行，转换期间其他上传与下载不受影响。
"""
from __future__ import annotations

import html
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import pipeline
from .store import STATUS_LABELS, Store

PAGE_CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
margin:2rem auto;max-width:960px;color:#222}
h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:2rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}
th{background:#f5f5f5}
.status-published{color:#137333;font-weight:600}
.status-rejected,.status-failed{color:#b3261e;font-weight:600}
.status-processing,.status-received{color:#b06000;font-weight:600}
form.inline{display:inline} input[type=text]{padding:.35rem;width:20rem}
button{padding:.35rem .9rem} .muted{color:#777;font-size:.85rem}
.reason{white-space:pre-wrap;word-break:break-all}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _page(title: str, body: str, refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (
        f"<!doctype html><html lang=zh><head><meta charset=utf-8>{meta}"
        f"<title>{_esc(title)}</title><style>{PAGE_CSS}</style></head>"
        f"<body><h1>{_esc(title)}</h1>{body}</body></html>"
    )


def _status_html(status: str) -> str:
    label = STATUS_LABELS.get(status, status)
    return f'<span class="status-{_esc(status)}">{_esc(label)}</span>'


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store(os.environ.get("DEID_DATA_DIR", "./data"))
    store.init()
    app = FastAPI(title="DICOM 去标识化平台")
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deid-worker")

    # ---------------- 网页 ----------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        rows = "".join(
            "<tr>"
            f'<td><a href="/projects/{_esc(p["project_id"])}">{_esc(p["project_id"])}</a></td>'
            f"<td>{_esc(p['name'])}</td>"
            f"<td>{p['package_count']}</td><td>{p['instance_count']}</td>"
            f"<td class=muted>{_esc(p['created_at'])}</td></tr>"
            for p in store.list_projects()
        ) or '<tr><td colspan=5 class=muted>暂无项目</td></tr>'
        body = f"""
        <h2>新建研究项目</h2>
        <form method=post action="/projects">
          <input type=text name=name placeholder="项目名称（如：肺癌纵向队列）" required>
          <button type=submit>创建</button>
        </form>
        <h2>项目列表</h2>
        <table><tr><th>项目ID</th><th>名称</th><th>包数</th><th>已发布实例</th><th>创建时间</th></tr>
        {rows}</table>"""
        return _page("DICOM 去标识化平台", body)

    @app.post("/projects")
    async def create_project(request: Request):
        form = await request.form()
        name = str(form.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "项目名称不能为空")
        project_id = store.create_project(name)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(project_id: str):
        project = store.get_project(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        packages = store.list_packages(project_id)
        rows = ""
        for p in packages:
            download = (
                f'<a href="/packages/{_esc(p["package_id"])}/download">下载处理包</a>'
                if p["status"] == "published" else ""
            )
            rows += (
                "<tr>"
                f"<td class=muted>{_esc(p['package_id'])}</td>"
                f"<td>{_esc(p['original_name'])}</td>"
                f"<td>{_status_html(p['status'])}</td>"
                f"<td>{p['instance_count']}</td>"
                f'<td class=reason>{_esc(p["reason"])}</td>'
                f"<td>{download}</td>"
                f"<td class=muted>{_esc(p['created_at'])}</td></tr>"
            )
        if not rows:
            rows = '<tr><td colspan=7 class=muted>暂无上传记录</td></tr>'
        busy = any(p["status"] in ("received", "processing") for p in packages)
        body = f"""
        <p class=muted><a href="/">&larr; 返回项目列表</a></p>
        <h2>上传检查 ZIP（1~20 份单帧未压缩 DICOM，不得含像素烧录文字）</h2>
        <form method=post action="/projects/{_esc(project_id)}/packages"
              enctype=multipart/form-data>
          <input type=file name=file accept=".zip" required>
          <button type=submit>上传</button>
        </form>
        <h2>上传记录（已发布实例共 {store.instance_count(project_id)} 个）</h2>
        <table><tr><th>包ID</th><th>原始文件</th><th>状态</th><th>实例数</th>
        <th>拒收/失败原因</th><th>操作</th><th>上传时间</th></tr>{rows}</table>"""
        return _page(f"项目 {project['name']}（{project_id}）", body, refresh=busy)

    @app.post("/projects/{project_id}/packages")
    def upload_package(project_id: str, file: UploadFile = File(...)):
        _save_and_enqueue(project_id, file)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.get("/packages/{package_id}/download")
    def download_package(package_id: str):
        pkg = store.get_package(package_id)
        if not pkg:
            raise HTTPException(404, "包不存在")
        if pkg["status"] != "published":
            raise HTTPException(
                409, f"包未发布（当前状态：{STATUS_LABELS.get(pkg['status'])}），"
                f"原因：{pkg.get('reason') or '无'}")
        path = store.packages_dir / f"{package_id}.zip"
        if not path.exists():
            raise HTTPException(404, "处理包文件缺失")
        return FileResponse(
            path, media_type="application/zip",
            filename=f"deidentified_{pkg['project_id']}_{package_id}.zip")

    # ---------------- JSON API ----------------
    @app.post("/api/projects")
    async def api_create_project(request: Request):
        payload = await request.json()
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, "项目名称不能为空")
        project_id = store.create_project(name)
        return {"project_id": project_id, "name": name}

    @app.get("/api/projects/{project_id}")
    def api_project(project_id: str):
        project = store.get_project(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        return {
            **project,
            "instance_count": store.instance_count(project_id),
            "packages": store.list_packages(project_id),
        }

    @app.post("/api/projects/{project_id}/packages")
    def api_upload(project_id: str, file: UploadFile = File(...)):
        package_id = _save_and_enqueue(project_id, file)
        return {"package_id": package_id}

    @app.get("/api/packages/{package_id}")
    def api_package(package_id: str):
        pkg = store.get_package(package_id)
        if not pkg:
            raise HTTPException(404, "包不存在")
        return pkg

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---------------- 公共逻辑 ----------------
    def _save_and_enqueue(project_id: str, file: UploadFile) -> str:
        if not store.get_project(project_id):
            raise HTTPException(404, "项目不存在")
        package_id = store.create_package(project_id, file.filename or "upload.zip")
        dest = store.originals_dir / f"{package_id}.zip"
        size = 0
        try:
            with dest.open("wb") as f:
                while chunk := file.file.read(1 << 20):
                    size += len(chunk)
                    if size > pipeline.MAX_UPLOAD_BYTES:
                        dest.unlink(missing_ok=True)
                        store.set_status(
                            package_id, "rejected",
                            "上传文件超过 512MiB 大小限制，整包拒收")
                        raise HTTPException(413, "上传文件超过 512MiB 大小限制")
                    f.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        except Exception:
            dest.unlink(missing_ok=True)
            store.set_status(package_id, "failed", "上传保存失败")
            raise
        executor.submit(pipeline.process_package, store, package_id)
        return package_id

    return app


app = create_app()
