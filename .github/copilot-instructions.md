# Canvas to Storage Sync - AI Coding Agent Instructions

## Project Overview

Single-file Python script (`main.py`) that syncs Canvas LMS content to Google Drive or local storage. Maintains course structure, only syncs changed content via smart timestamp/size comparison. Key constraint: Most institutions disable Canvas Files API, so we discover files via module/page links only.

## Architecture & Critical Patterns

### Main Processing Flow

1. Load config → authenticate Canvas/Drive → fetch courses
2. User selects courses (remembers last selection in `config.ini`)
3. For each course: process assignments → modules → pages → linked files
4. Track changes in `SummaryCollector`, print report, cleanup temp files

**Shared HTTP Session Pattern**: Use `session` parameter throughout to reuse connection pool (`HTTPAdapter` with retry logic). Never create new sessions in helper functions.

```python
# ALWAYS pass session through the call chain
process_canvas_file(..., session=session, timeout=request_timeout)
```

### HTML → PDF Conversion (`html_to_pdf_elements()`)

- **BeautifulSoup** parses Canvas HTML; **ReportLab** generates styled PDFs
- Handles headings, lists, links, code blocks, blockquotes
- **Critical**: Accumulates inline content in `inline_buffer`, flushes to paragraph when block element encountered
- **Fallback**: If HTML parsing produces nothing, extract plain text with `BeautifulSoup.get_text()`

```python
# Pattern for adding HTML content to PDF
html_elements = html_to_pdf_elements(description, styles)
content.extend(html_elements)  # Never append single element, always extend
```

### Change Detection (`has_file_changed()`)

**Two-phase check**: (1) Compare `size`, (2) Compare `updated_at` timestamps (ISO format for Canvas/Drive, epoch for local).

```python
# Check before download/upload to avoid redundant work
if not has_file_changed(existing_metadata, canvas_size=file_size, canvas_updated_at=updated_at):
    return 0  # Skip processing
```

**Metadata retrieval**: Always use `get_existing_file_metadata_drive()` or `get_existing_file_metadata_local()` before processing files.

### File Discovery Strategy

- **Assignments**: Scan description HTML for `/files/(\d+)` links
- **Pages**: Dual-source discovery (Pages API + modules) to catch all pages; scan body HTML for file links
- **Modules**: Iterate items, handle `File` and `Page` types explicitly
- **Result**: `processed_canvas_file_ids` set prevents duplicate processing

### PDF Generation Patterns

**Assignments**: Title → due date → points → rubric (with criteria/ratings) → description  
**Pages** (individual): Title → "View on Canvas" link → body  
**Pages** (merged): All course pages in single PDF with TOC and internal links (uses custom `TOCDocTemplate`)

Always wrap user content in `html.escape()` before adding to Paragraph:

```python
escaped_title = html.escape(assignment_name, quote=False)
content.append(Paragraph(escaped_title, title_style))
```

## Critical Developer Workflows

### Setup & Running

```powershell
# Install dependencies
pip install -r requirements.txt

# Configure Canvas API (config.ini)
[CANVAS]
API_URL = https://school.instructure.com
API_KEY = <token>

# Run script
python main.py
```

**First Google Drive run**: Opens browser for OAuth2 consent → creates `token.json`

### Testing Changes

1. Create test course with varied content (HTML tables, images, nested lists)
2. Run with `STORAGE_TYPE=local` first (faster iteration)
3. Test change detection: run twice, verify "already up to date" message
4. Switch to Google Drive, verify same behavior with `get_existing_file_metadata_drive()`

### Debugging Canvas API Issues

- **401 Unauthorized**: Check `API_KEY` in config.ini
- **404 Not Found**: Institution may have disabled endpoint (e.g., files); adjust discovery logic
- **Rate limits**: Increase `BACKOFF_FACTOR` in `[PERFORMANCE]` section
- **Pagination errors**: Verify `Link` header parsing in `get_paginated_canvas_items()`

## Project-Specific Conventions

### Error Handling

```python
try:
    response.raise_for_status()
    # Process response
except requests.RequestException as e:
    print(f"Error: {e}")  # Always print user-friendly message
    return 0  # Return count to track failures
```

**Never suppress errors silently** unless `suppress_errors=True` parameter is passed (used for optional endpoints like Pages API).

### File Naming

```python
safe_name = sanitize_filename(name)  # Removes \/*?:"<>| and strips whitespace
```

**Always sanitize** before using names for folders/files.

### API Pagination (Canvas)

```python
items = get_paginated_canvas_items(url, headers, session, timeout, per_page)
```

This helper handles `Link` header parsing and multi-page requests. Use `per_page` param to reduce round-trips (Canvas max: 100).

### Timestamp Handling

- **Canvas**: ISO 8601 with `Z` suffix (e.g., `2024-11-29T12:00:00Z`)
- **Google Drive**: ISO 8601 with timezone (e.g., `2024-11-29T12:00:00+00:00`)
- **Local**: POSIX epoch float (from `os.path.getmtime()`)

Use `_parse_iso_utc()` and `_to_utc_datetime()` helpers for normalization.

## Integration Points

### Canvas API

- **Auth**: `Authorization: Bearer <API_KEY>`
- **Endpoints**: `/api/v1/courses`, `/assignments`, `/modules`, `/pages`, `/files/{id}`
- **Pagination**: Parse `Link` header for `rel="next"`
- **Performance tuning**: Adjust `CANVAS_PER_PAGE` (default 100) in config

### Google Drive API

- **Scopes**: `["https://www.googleapis.com/auth/drive"]`
- **Auth flow**: OAuth2 via `credentials.json` → `token.json` (auto-refresh)
- **Folder operations**: `get_or_create_folder()` uses query `name='...' and mimeType='application/vnd.google-apps.folder'`
- **Upload**: `MediaFileUpload` with resumable=True, configurable `chunksize` (default 8MB)

### Local Storage

- **Root**: `LOCAL_ROOT_DIR` from config.ini
- **Operations**: `os.makedirs(exist_ok=True)`, `shutil.move()`
- **Metadata**: `os.path.getsize()`, `os.path.getmtime()` (epoch seconds)

## Key Files & Roles

- **`main.py`**: Entire application (1800+ lines, no modules)
- **`config.ini`**: Runtime config (API keys, storage type, last selection, perf tuning)
- **`credentials.json`**: Google OAuth2 client secret (user provides, not committed)
- **`token.json`**: Google OAuth2 refresh token (auto-generated, gitignored)
- **`temp_canvas_downloads/`**: Temp storage, cleared before/after each run
- **`CanvasSync.spec`**: PyInstaller config for building standalone `.exe`

## Common Pitfalls & Solutions

### Issue: Files not syncing

- **Cause**: Files not linked in modules/pages/assignments
- **Solution**: Explain to user that Canvas Files API is disabled; only linked files are discovered

### Issue: PDF generation fails with `ValueError`

- **Cause**: Malformed HTML (unclosed tags, invalid entities)
- **Solution**: Wrap `html_to_pdf_elements()` calls in try/except, use fallback plain text extraction

### Issue: Duplicate file downloads

- **Cause**: Not tracking `processed_canvas_file_ids`
- **Solution**: Always pass set through function calls, add file ID before processing

### Issue: Google Drive quota exceeded

- **Cause**: Uploading entire files repeatedly instead of using change detection
- **Solution**: Always call `get_existing_file_metadata_drive()` and check `has_file_changed()` before upload

### Issue: Performance degradation with large courses

- **Solution**: Tune `[PERFORMANCE]` config: increase `HTTP_POOL_MAXSIZE`, `CANVAS_PER_PAGE`; reduce `REQUEST_TIMEOUT`

## Adding New Features

1. **New content type**: Add processing function following pattern of `process_canvas_assignment()` / `process_canvas_file()`
2. **New storage backend**: Implement trio of functions: `get_or_create_folder_X()`, `get_existing_file_metadata_X()`, `save_file_X()`
3. **New PDF format**: Extend `html_to_pdf_elements()` with new tag handlers in `process_element()` recursive function

## Testing Without Canvas Account

Mock responses using `requests-mock` library:

```python
import requests_mock
with requests_mock.Mocker() as m:
    m.get('https://canvas.instructure.com/api/v1/courses', json=[...])
    # Run sync logic
```
