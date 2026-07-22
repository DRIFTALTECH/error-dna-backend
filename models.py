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


class URLResponse(BaseModel):
    id: int
    source_id: str
    title: Optional[str]
    source_url: str
    component: Optional[str]
    category: Optional[str]
    priority: Optional[str]
    released_on: Optional[str]
    status: str
    scraped_at: Optional[str]
    error_message: Optional[str]
    created_at: Optional[str]


# ---- Credential Models ----

class CredentialAdd(BaseModel):
    login_url: str
    username: str
    password: str


class CredentialUpdate(BaseModel):
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class CredentialResponse(BaseModel):
    id: int
    label: str
    login_url: str
    username_masked: str
    status: str
    usage_today: int
    last_used_at: Optional[str]
    is_active: bool
    is_locked: bool
    created_at: Optional[str]


# ---- Summary Models ----

class SummaryResponse(BaseModel):
    id: int
    source_id: str
    title: str
    family: Optional[str]
    area: Optional[str]
    type: Optional[str]
    issue: Optional[str]
    summary: Optional[str]
    steps: Optional[str]
    gotchas: Optional[str]
    tags: Optional[str]
    source_version: Optional[int]
    source_date: Optional[str]
    source_url: Optional[str]
    component: Optional[str]
    environment: Optional[str]
    is_latest: bool
    verification_status: str
    created_at: Optional[str]


# ---- Scheduler Models ----

class SchedulerUpdate(BaseModel):
    min_delay_min: Optional[int] = None
    max_delay_min: Optional[int] = None


class SchedulerResponse(BaseModel):
    id: int
    min_delay_min: int
    max_delay_min: int
    is_paused: bool
    next_scrape_at: Optional[str]
    remaining_seconds: Optional[int]
    active_account_label: Optional[str]
    today_scraped: int
    today_failed: int
    total_pending: int


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
