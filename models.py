"""Pydantic models for request/response validation."""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ---- URL Models ----

class URLAdd(BaseModel):
    source_id: str
    title: Optional[str] = None
    source_url: str
    component: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    released_on: Optional[str] = None


class URLUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None


# ---- Credential Models ----

class CredentialAdd(BaseModel):
    login_url: str
    username: str
    password: str


class CredentialUpdate(BaseModel):
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


# ---- Summary Models ----

# ---- Scheduler Models ----

class SchedulerUpdate(BaseModel):
    min_delay_min: Optional[int] = None
    max_delay_min: Optional[int] = None


# ---- Health Check ----

class HealthResponse(BaseModel):
    status: str
    db: str
    version: str
    timestamp: str


# ---- Pagination ----

class PaginatedResponse(BaseModel):
    data: list
    total: int
    page: int
    page_size: int
    total_pages: int


# ---- Upload Response ----

class UploadResponse(BaseModel):
    imported: int
    duplicates: int
    total_rows: int
    message: str


# ---- Dashboard ----

class DashboardResponse(BaseModel):
    total_urls: int
    completed: int
    pending: int
    failed: int
    skipped: int
    summaries_count: int
    recent_summaries: list
    families: list
