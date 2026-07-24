"""URL management routes — Excel upload, CRUD, filtering."""

import asyncio
import io
from fastapi import APIRouter, UploadFile, File, Query, HTTPException
from db import read, write
from models import URLAdd, URLUpdate, UploadResponse

router = APIRouter(prefix="/api/urls", tags=["urls"])


def _parse_xlsx_rows(content: bytes) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    try:
        ws = wb.active
        out = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            cell = lambda i: str(row[i]).strip() if len(row) > i and row[i] else ""
            out.append({
                "component": cell(0),
                "source_id": cell(1),
                "title": cell(3),
                "category": cell(4),
                "priority": cell(5),
                "released_on": cell(6),
                "source_url": cell(7),
            })
        return out
    finally:
        wb.close()


@router.get("")
async def list_urls(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str = Query(None),
    category: str = Query(None),
    search: str = Query(None),
):
    where_clauses, params = [], []
    if status:
        where_clauses.append("status = ?"); params.append(status)
    if category:
        where_clauses.append("category = ?"); params.append(category)
    if search:
        where_clauses.append("(title LIKE ? OR source_id LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    offset = (page - 1) * page_size

    total = (await read(f"SELECT COUNT(*) as c FROM urls {where_sql}", params))[0]["c"]
    rows = await read(
        f"SELECT * FROM urls {where_sql} ORDER BY id ASC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    items = [{**r, "note_number": r.get("source_id")} for r in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.post("")
async def add_url(body: URLAdd):
    try:
        rows = await write(
            """INSERT INTO urls (source_id, title, source_url, component, category, priority, released_on)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (body.source_id, body.title, body.source_url, body.component,
             body.category, body.priority, body.released_on),
        )
        return {"ok": True, "id": rows[0]["id"]}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/upload", response_model=UploadResponse)
async def upload_excel(file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx or .xls files allowed")

    try:
        content = await file.read()
        rows = await asyncio.to_thread(_parse_xlsx_rows, content)
        total_rows = len(rows)

        existing = {r["source_id"] for r in await read("SELECT source_id FROM urls")}
        imported = duplicates = 0
        for r in rows:
            if not r["source_id"] or not r["source_url"]:
                continue
            if r["source_id"] in existing:
                duplicates += 1
                continue
            existing.add(r["source_id"])
            await write(
                """INSERT INTO urls (source_id, title, source_url, component, category, priority, released_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["source_id"], r["title"], r["source_url"], r["component"],
                 r["category"], r["priority"], r["released_on"]),
            )
            imported += 1

        return UploadResponse(
            imported=imported,
            duplicates=duplicates,
            total_rows=total_rows,
            message=f"Imported {imported} URLs. {duplicates} duplicates skipped.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {str(e)}")


@router.patch("/{url_id}")
async def update_url(url_id: int, body: URLUpdate):
    sets, params = [], []
    if body.status is not None:
        sets.append("status = ?"); params.append(body.status)
    if body.title is not None:
        sets.append("title = ?"); params.append(body.title)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = datetime('now', 'localtime')")
    params.append(url_id)
    await write(f"UPDATE urls SET {', '.join(sets)} WHERE id = ?", params)
    return {"ok": True}


@router.delete("/{url_id}")
async def delete_url(url_id: int):
    await write("DELETE FROM urls WHERE id = ?", (url_id,))
    return {"ok": True}
