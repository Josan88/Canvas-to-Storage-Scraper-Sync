# Canvas to Storage Sync - AI Agent Instructions

## Project Overview

This is a Python script that syncs Canvas LMS course content (files, pages, assignments) to either Google Drive or local storage. The script maintains course structure and only syncs changed content.

## Architecture & Key Components

### Core Flow (`main()` function)

- **Configuration**: Load settings from `config.ini` (Canvas API URL/key, storage type)
- **Authentication**: Initialize Canvas API headers + Google Drive service (if needed)
- **Course Selection**: Interactive selection with memory of last choices
- **Processing Loop**: For each course → assignments → modules/pages → files
- **Cleanup**: Remove temp files, report sync status

### Data Processing Patterns

#### HTML → PDF Conversion (`html_to_pdf_elements()`)

- Uses BeautifulSoup for HTML parsing
- ReportLab for PDF generation with custom styles
- Preserves formatting: headings, lists, links, code blocks
- **Example pattern**:

```python
# Convert HTML description to PDF elements
html_elements = html_to_pdf_elements(description, styles)
content.extend(html_elements)
```

#### File Change Detection (`has_file_changed()`)

- Compares file size and modification timestamps
- Handles both local and Google Drive metadata
- **Pattern**: Always check before downloading/uploading

#### Folder Structure Creation

```
Root Storage/
├── Course Name/
│   ├── Assignments/
│   │   └── Assignment Name.pdf
│   ├── Page Name/
│   │   ├── Page Name.pdf
│   │   └── linked-file.pdf
│   └── direct-module-files/
```

## Critical Developer Workflows

### Setup & Dependencies

```bash
# Install requirements
pip install -r requirements.txt

# Configure APIs
# 1. Copy config.ini.example → config.ini
# 2. Add Canvas API key and URL
# 3. For Google Drive: Download credentials.json
```

### Running the Script

```bash
python main.py
```

- First Google Drive run: Browser auth creates `token.json`
- Interactive course selection with "last" option
- Progress logging for each course/assignment/page

### Debugging Common Issues

- **Canvas API errors**: Check API key and URL in config.ini
- **Google Drive auth**: Delete token.json to re-authenticate
- **File not found**: Many institutions disable Files tab - script finds linked files only
- **PDF generation fails**: Check HTML content for malformed tags

## Project-Specific Conventions

### Error Handling Pattern

```python
try:
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    # Process response
except requests.RequestException as e:
    print(f"Error message: {e}")
```

### File Naming & Sanitization

```python
safe_name = sanitize_filename(name)  # Removes invalid chars: \/ *?:"<>|
filename = f"{safe_name}.pdf"
```

### API Pagination Handling

```python
items = []
next_url = initial_url
while next_url:
    response = requests.get(next_url, headers=headers)
    items.extend(response.json())
    next_url = get_next_page_url(response.headers)
```

### Configuration Management

- Uses ConfigParser for INI files
- Sections: [CANVAS], [STORAGE], [LAST_SELECTION]
- **Pattern**: Check config existence before reading

## Integration Points

### Canvas API

- **Base URL**: From config.ini (e.g., https://school.instructure.com)
- **Auth**: Bearer token in headers
- **Endpoints**: /api/v1/courses, /assignments, /modules, /files
- **Pagination**: Link header with rel="next"

### Google Drive API

- **Scopes**: ["https://www.googleapis.com/auth/drive"]
- **Auth flow**: OAuth2 with credentials.json → token.json
- **Operations**: Create folders, upload files, check existence
- **File metadata**: ID, size, modifiedTime

### Local Storage

- **Root**: Configurable directory path
- **Operations**: os.makedirs(), shutil.move(), os.path.exists()
- **Metadata**: os.path.getsize(), os.path.getmtime()

## Key Files & Their Roles

- **`main.py`**: Single-file application with all logic
- **`config.ini`**: Runtime configuration (API keys, storage settings)
- **`credentials.json`**: Google OAuth2 credentials (user provides)
- **`token.json`**: Google OAuth2 refresh token (auto-generated)
- **`requirements.txt`**: Python dependencies
- **`temp_canvas_downloads/`**: Temporary file storage during sync

## Development Best Practices

### Code Organization

- Helper functions at top, main logic at bottom
- Group related functions together (API helpers, file helpers, etc.)
- Use descriptive variable names (canvas_headers, drive_service)

### Testing Approach

- Manual testing: Run script with small course selection
- Check both storage types (local + Google Drive)
- Verify PDF generation with complex HTML content
- Test error scenarios (invalid API keys, network issues)

### Maintenance Notes

- Script handles Canvas API pagination automatically
- File change detection prevents unnecessary downloads
- Course selection memory improves UX for repeated runs
- HTML parsing is robust but may need updates for unusual content
