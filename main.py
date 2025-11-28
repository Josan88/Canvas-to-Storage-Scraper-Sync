import os
import requests
import configparser
import shutil
import re
from typing import Optional, Dict, List, DefaultDict
from collections import defaultdict
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from bs4.element import Tag, NavigableString
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem,
    PageBreak,
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib.units import inch
from reportlab.lib.colors import blue, HexColor
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from datetime import datetime
import html
import json
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


# --- Configuration ---
SCOPES = ["https://www.googleapis.com/auth/drive"]
CONFIG_FILE = "config.ini"
GOOGLE_CREDS_FILE = "credentials.json"
GOOGLE_TOKEN_FILE = "token.json"
DOWNLOAD_DIR = "temp_canvas_downloads"

# Performance tuning defaults (overridable via config.ini [PERFORMANCE])
DEFAULT_REQUEST_TIMEOUT = 20  # seconds
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_CANVAS_PER_PAGE = 100
DEFAULT_HTTP_POOL_MAXSIZE = 20
DEFAULT_DRIVE_CHUNK_SIZE_MB = 8


# --- Helper Functions ---
class SummaryCollector:
    """Collects a per-course summary of updated/created files grouped by destination folder label."""

    def __init__(self):
        # Structure: { course_name: { dest_label: [ (filename, action) ] } }
        self.per_course: Dict[str, DefaultDict[str, List[tuple]]] = {}

    def add_file(self, course_name: str, dest_label: str, filename: str, action: str):
        if not course_name or not dest_label or not filename:
            return
        if course_name not in self.per_course:
            self.per_course[course_name] = defaultdict(list)
        self.per_course[course_name][dest_label].append((filename, action))

    def has_changes(self) -> bool:
        return any(self.per_course.get(c) for c in self.per_course)

    def print_summary(self):
        print("\n=== Summary of Updates ===")
        if not self.has_changes():
            print("No files or folders were updated across the selected courses.")
            return
        for course_name, folders in self.per_course.items():
            print(f"\nCourse: {course_name}")
            for dest_label, items in folders.items():
                print(f"  Folder: {dest_label}")
                for filename, action in items:
                    print(f"    - {filename}  [{action}]")
        print("\n==========================")


def sanitize_filename(name):
    """Removes invalid characters from a string to make it a valid filename."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def get_existing_file_metadata_drive(service, folder_id, filename):
    """Gets metadata of an existing file in Google Drive folder."""
    if not folder_id or not filename:
        return None
    try:
        escaped_name = filename.replace("'", "\\'")
        query = f"name='{escaped_name}' and '{folder_id}' in parents and trashed=false"
        response = (
            service.files()
            .list(q=query, fields="files(id, size, modifiedTime)")
            .execute()
        )
        files = response.get("files", [])
        if files:
            file = files[0]  # Take the first if multiple
            return {
                "id": file.get("id"),
                "size": int(file.get("size", 0)) if file.get("size") else 0,
                "modified_time": file.get("modifiedTime"),
            }
    except HttpError as error:
        print(f"Error fetching metadata for '{filename}': {error}")
    return None


def get_existing_file_metadata_local(folder_path, filename):
    """Gets metadata of an existing file in local folder."""
    if not folder_path or not filename:
        return None
    path = os.path.join(folder_path, filename)
    if os.path.exists(path):
        try:
            return {
                "size": os.path.getsize(path),
                "modified_time": os.path.getmtime(path),
            }
        except OSError as error:
            print(f"Error getting metadata for '{path}': {error}")
    return None


def has_file_changed(existing_metadata, canvas_size=None, canvas_updated_at=None):
    """Checks if file has changed based on metadata."""
    if not existing_metadata:
        return True  # New file
    if canvas_size is not None and existing_metadata["size"] != canvas_size:
        return True
    if canvas_updated_at and existing_metadata["modified_time"]:
        # Compare timestamps, assuming canvas_updated_at is ISO format
        from datetime import datetime

        try:
            canvas_time = datetime.fromisoformat(
                canvas_updated_at.replace("Z", "+00:00")
            )
            existing_time = datetime.fromisoformat(
                existing_metadata["modified_time"].replace("Z", "+00:00")
            )
            if canvas_time > existing_time:
                return True
        except ValueError:
            pass  # If parsing fails, assume changed
    return False


def html_to_pdf_elements(html_content, base_styles):
    """Convert HTML content to ReportLab flowables with formatting preserved."""
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    elements = []
    inline_buffer = ""  # Accumulates inline-only content to wrap into a paragraph

    # Define styles for different HTML elements
    styles = {
        "p": base_styles["Normal"],
        "h1": ParagraphStyle(
            "h1", parent=base_styles["Heading1"], fontSize=18, spaceAfter=20
        ),
        "h2": ParagraphStyle(
            "h2", parent=base_styles["Heading2"], fontSize=16, spaceAfter=18
        ),
        "h3": ParagraphStyle(
            "h3", parent=base_styles["Heading3"], fontSize=14, spaceAfter=16
        ),
        "h4": ParagraphStyle(
            "h4", parent=base_styles["Heading4"], fontSize=12, spaceAfter=14
        ),
        "h5": ParagraphStyle(
            "h5",
            parent=base_styles["Normal"],
            fontSize=11,
            fontName="Helvetica-Bold",
            spaceAfter=12,
        ),
        "h6": ParagraphStyle(
            "h6",
            parent=base_styles["Normal"],
            fontSize=10,
            fontName="Helvetica-Bold",
            spaceAfter=10,
        ),
        "strong": ParagraphStyle(
            "strong", parent=base_styles["Normal"], fontName="Helvetica-Bold"
        ),
        "b": ParagraphStyle(
            "b", parent=base_styles["Normal"], fontName="Helvetica-Bold"
        ),
        "em": ParagraphStyle(
            "em", parent=base_styles["Normal"], fontName="Helvetica-Oblique"
        ),
        "i": ParagraphStyle(
            "i", parent=base_styles["Normal"], fontName="Helvetica-Oblique"
        ),
        "u": ParagraphStyle("u", parent=base_styles["Normal"], underline=True),
        "blockquote": ParagraphStyle(
            "blockquote", parent=base_styles["Normal"], leftIndent=20, rightIndent=20
        ),
        "code": ParagraphStyle(
            "code",
            parent=base_styles["Normal"],
            fontName="Courier",
            fontSize=9,
            backColor=HexColor("#f0f0f0"),
        ),
        "pre": ParagraphStyle(
            "pre",
            parent=base_styles["Normal"],
            fontName="Courier",
            fontSize=9,
            leftIndent=10,
        ),
        "li": ParagraphStyle(
            "li", parent=base_styles["Normal"], leftIndent=15, bulletIndent=5
        ),
    }

    def process_element(element, current_style=None):
        """Recursively process HTML elements and convert to formatted text."""
        if element is None:
            return ""

        if element.name is None:  # Text node
            text = element.string
            if text and text.strip():
                # Escape HTML entities to prevent parsing errors
                text = html.escape(text, quote=False)
                if current_style:
                    return f'<font name="{current_style.fontName}" size="{current_style.fontSize}">{text}</font>'
                else:
                    return text
            return ""

        # Handle different HTML elements
        tag_name = element.name.lower()

        if tag_name in ["p", "div"]:
            content = ""
            for child in element.children:
                child_result = process_element(child, current_style)
                if child_result is not None:
                    content += child_result
            if content.strip():
                style = current_style or styles.get("p", base_styles["Normal"])
                elements.append(Paragraph(content, style))
                elements.append(Spacer(1, 6))
            return ""

        elif tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            content = ""
            for child in element.children:
                child_result = process_element(
                    child, styles.get(tag_name, base_styles["Normal"])
                )
                if child_result is not None:
                    content += child_result
            if content.strip():
                elements.append(
                    Paragraph(content, styles.get(tag_name, base_styles["Normal"]))
                )
                elements.append(Spacer(1, 12))
            return ""

        elif tag_name in ["strong", "b"]:
            content = ""
            for child in element.children:
                content += process_element(
                    child, styles.get("strong", base_styles["Normal"])
                )
            return content

        elif tag_name in ["em", "i"]:
            content = ""
            for child in element.children:
                content += process_element(
                    child, styles.get("em", base_styles["Normal"])
                )
            return content

        elif tag_name == "u":
            content = ""
            for child in element.children:
                content += process_element(
                    child, styles.get("u", base_styles["Normal"])
                )
            return content

        elif tag_name == "br":
            return "<br/>"

        elif tag_name == "a":
            href = element.get("href", "")
            content = ""
            for child in element.children:
                content += process_element(child, current_style)
            if content.strip():
                # Skip anchor links that cause PDF generation issues
                if href.startswith("#"):
                    return content
                # Create a link style
                link_style = ParagraphStyle(
                    "link",
                    parent=current_style or base_styles["Normal"],
                    textColor=blue,
                    underline=True,
                )
                return f'<link href="{href}">{content}</link>'
            return content

        elif tag_name in ["ul", "ol"]:
            list_items = []
            for li in element.find_all("li", recursive=False):
                li_content = ""
                for child in li.children:
                    child_result = process_element(
                        child, styles.get("li", base_styles["Normal"])
                    )
                    if child_result is not None:
                        li_content += child_result
                if li_content.strip():
                    list_items.append(
                        Paragraph(li_content, styles.get("li", base_styles["Normal"]))
                    )

            if list_items:
                if tag_name == "ul":
                    elements.append(
                        ListFlowable(list_items, bulletType="bullet", start="•")
                    )
                else:  # ol
                    elements.append(ListFlowable(list_items, bulletType="1"))
                elements.append(Spacer(1, 6))
            return ""

        elif tag_name == "li":
            # This should be handled by ul/ol processing above
            content = ""
            for child in element.children:
                content += process_element(child, current_style)
            return content

        elif tag_name == "blockquote":
            content = ""
            for child in element.children:
                child_result = process_element(
                    child, styles.get("blockquote", base_styles["Normal"])
                )
                if child_result is not None:
                    content += child_result
            if content.strip():
                elements.append(
                    Paragraph(content, styles.get("blockquote", base_styles["Normal"]))
                )
                elements.append(Spacer(1, 6))
            return ""

        elif tag_name in ["code", "pre"]:
            content = element.get_text()
            if content.strip():
                elements.append(
                    Paragraph(content, styles.get(tag_name, base_styles["Normal"]))
                )
                elements.append(Spacer(1, 6))
            return ""

        else:
            # For unknown tags, process children
            content = ""
            for child in element.children:
                child_result = process_element(child, current_style)
                if child_result is not None:
                    content += child_result
            return content

    # Process all top-level elements
    def is_block_tag(name: str) -> bool:
        if not name:
            return False
        name = name.lower()
        return name in {
            "p",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "li",
            "blockquote",
            "pre",
            "code",
        }

    for element in soup.children:
        # If we encounter a block-level element, flush any accumulated inline content first
        if isinstance(element, Tag) and is_block_tag(element.name):
            if inline_buffer.strip():
                elements.append(Paragraph(inline_buffer, base_styles["Normal"]))
                elements.append(Spacer(1, 6))
                inline_buffer = ""
            process_element(element)
            continue

        # Otherwise, collect inline content and wrap later as a paragraph
        if isinstance(element, (Tag, NavigableString)):
            returned_text = process_element(element)
            if isinstance(returned_text, str) and returned_text.strip():
                inline_buffer += returned_text

    # Flush any remaining inline content as a final paragraph
    if inline_buffer.strip():
        elements.append(Paragraph(inline_buffer, base_styles["Normal"]))

    return elements


def display_courses_and_get_selection(courses, last_course_ids=None):
    """Displays available courses and gets user selection."""
    print("\nAvailable courses:")
    for i, course in enumerate(courses, 1):
        course_name = course.get("name", "Unnamed")
        course_code = course.get("course_code", "")
        marker = (
            " (last selected)"
            if last_course_ids and str(course.get("id")) in last_course_ids
            else ""
        )
        print(f"{i}. {course_name} ({course_code}){marker}")

    print("\nOptions:")
    print("- Enter course numbers separated by commas (e.g., 1,3,5)")
    print("- Enter 'all' to select all courses")
    print("- Enter 'last' to use last selection" if last_course_ids else "")
    print("- Enter 'quit' to exit")

    while True:
        try:
            user_input = input("\nSelect courses to sync: ").strip().lower()

            if user_input == "quit":
                return []

            if user_input == "all":
                return courses

            if user_input == "last" and last_course_ids:
                # Find courses that match the last selected IDs

                last_courses = [
                    course
                    for course in courses
                    if str(course.get("id")) in last_course_ids
                ]
                if last_courses:
                    print(f"Using last selection: {len(last_courses)} course(s)")
                    return last_courses
                else:
                    print(
                        "Last selected courses are no longer available. Please select manually."
                    )
                    continue

            # Parse comma-separated numbers
            selections = []
            for part in user_input.split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(courses):
                        selections.append(courses[idx])
                    else:
                        print(f"Invalid course number: {int(part)}")
                        selections = []
                        break
                else:
                    print(f"Invalid input: {part}")
                    selections = []
                    break

            if selections:
                return selections
            else:
                print("No valid courses selected. Please try again.")

        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            return []
        except Exception as e:
            print(f"Error processing selection: {e}")
            return []


def save_last_selection(selected_courses):
    """Saves the selected course IDs to config file."""
    if not selected_courses:
        return

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    if not config.has_section("LAST_SELECTION"):
        config.add_section("LAST_SELECTION")

    course_ids = [
        str(course.get("id")) for course in selected_courses if course.get("id")
    ]
    config.set("LAST_SELECTION", "COURSE_IDS", ",".join(course_ids))

    with open(CONFIG_FILE, "w") as configfile:
        config.write(configfile)


def load_last_selection():
    """Loads the last selected course IDs from config file."""
    if not os.path.exists(CONFIG_FILE):
        return None

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    if config.has_section("LAST_SELECTION") and config.has_option(
        "LAST_SELECTION", "COURSE_IDS"
    ):
        course_ids_str = config.get("LAST_SELECTION", "COURSE_IDS").strip()
        if course_ids_str:
            return set(course_ids_str.split(","))

    return None


def get_canvas_quizzes(
    course_id: int,
    session: requests.Session,
    api_url: str,
    api_key: str,
    timeout: int = 30,
    per_page: int = 100,
) -> list:
    """
    Fetch quizzes for a given Canvas course using the Quizzes API.
    Returns a list of quiz dicts, or empty list on error.
    """
    url = f"{api_url}/api/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {api_key}"}
    quizzes = []
    params = {"per_page": per_page}
    try:
        while url:
            response = session.get(url, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            quizzes.extend(data)
            # Handle pagination
            link = response.headers.get("Link", "")
            next_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part[part.find("<") + 1 : part.find(">")]
                    break
            url = next_url
            params = {}  # Only use params on first request
    except requests.RequestException as e:
        print(f"Error fetching quizzes for course {course_id}: {e}")
        return []
    return quizzes


# --- Google Drive Service Functions ---


def get_drive_service():
    """Authenticates with the Google Drive API and returns a service object."""
    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Could not refresh token: {e}. Re-authenticating...")
                os.remove(GOOGLE_TOKEN_FILE)
                return get_drive_service()
        else:
            if not os.path.exists(GOOGLE_CREDS_FILE):
                print(
                    f"ERROR: Google credentials file '{GOOGLE_CREDS_FILE}' not found."
                )
                return None
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    try:
        return build("drive", "v3", credentials=creds)
    except HttpError as error:
        print(f"An error occurred building Drive service: {error}")
        return None


def get_or_create_folder(service, folder_name, parent_id=None):
    """Finds a folder by name. If not found, creates it. Returns the folder ID."""
    # Escape single quotes in folder_name for query
    escaped_name = folder_name.replace("'", "\\'")
    query = f"name='{escaped_name}' and mimeType='application/vnd.google-apps.folder'"
    query += f" and '{parent_id}' in parents" if parent_id else " and 'root' in parents"
    try:
        response = (
            service.files().list(q=query, spaces="drive", fields="files(id)").execute()
        )
        folders = response.get("files", [])
        if folders:
            return folders[0].get("id")
        else:
            print(f"Creating Google Drive folder: '{folder_name}'...")
            file_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                file_metadata["parents"] = [parent_id]
            folder = service.files().create(body=file_metadata, fields="id").execute()
            return folder.get("id")
    except HttpError as error:
        print(f"Error finding/creating folder '{folder_name}': {error}")
        return None


def get_existing_files_in_drive_folder(service, folder_id):
    """Returns a set of filenames that already exist in a Drive folder."""
    if not folder_id:
        return set()
    try:
        response = (
            service.files()
            .list(q=f"'{folder_id}' in parents", spaces="drive", fields="files(name)")
            .execute()
        )
        return {item["name"] for item in response.get("files", [])}
    except HttpError as error:
        print(f"Error fetching files from Drive folder: {error}")
        return set()


def upload_file_to_drive(
    service,
    local_path,
    drive_filename,
    folder_id,
    existing_file_id=None,
    drive_chunk_size_mb: int = DEFAULT_DRIVE_CHUNK_SIZE_MB,
):
    """Uploads a single file to the specified Google Drive folder, or updates if existing_file_id provided."""
    if not os.path.exists(local_path):
        return False
    try:
        chunk_bytes = max(256 * 1024, drive_chunk_size_mb * 1024 * 1024)
        if existing_file_id:
            print(f"Updating '{drive_filename}' in Google Drive...")
            media = MediaFileUpload(local_path, chunksize=chunk_bytes, resumable=True)
            service.files().update(fileId=existing_file_id, media_body=media).execute()
        else:
            print(f"Uploading '{drive_filename}' to Google Drive...")
            file_metadata = {"name": drive_filename, "parents": [folder_id]}
            # Specify mimetype for HTML files for better browser handling
            mimetype = "text/html" if drive_filename.lower().endswith(".html") else None
            media = MediaFileUpload(
                local_path, mimetype=mimetype, chunksize=chunk_bytes, resumable=True
            )
            service.files().create(
                body=file_metadata, media_body=media, fields="id"
            ).execute()
        return True
    except HttpError as error:
        print(f"An error occurred during file upload/update: {error}")
        return False


# --- Local Storage Functions ---


def get_or_create_local_folder(local_root_dir, folder_name, parent_path=None):
    """Creates a local folder if it doesn't exist. Returns the full path."""
    if parent_path:
        folder_path = os.path.join(parent_path, folder_name)
    else:
        folder_path = os.path.join(local_root_dir, folder_name)

    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        print(f"Created local folder: '{folder_path}'")
    return folder_path


def get_existing_files_in_local_folder(folder_path):
    """Returns a set of filenames that already exist in a local folder."""
    if not os.path.exists(folder_path):
        return set()
    try:
        return {
            f
            for f in os.listdir(folder_path)
            if os.path.isfile(os.path.join(folder_path, f))
        }
    except OSError as error:
        print(f"Error reading local folder '{folder_path}': {error}")
        return set()


def save_file_locally(local_path, filename, folder_path):
    """Moves a file from temp directory to the specified local folder."""
    if not os.path.exists(local_path):
        return False
    try:
        destination_path = os.path.join(folder_path, filename)
        shutil.move(local_path, destination_path)
        print(f"Saved '{filename}' to local storage: '{folder_path}'")
        return True
    except OSError as error:
        print(f"An error occurred saving file locally: {error}")
        return False


def get_paginated_canvas_items(
    url,
    headers,
    session: Optional[requests.Session],
    timeout: int,
    per_page: int,
    suppress_errors: bool = False,
):
    """Handles Canvas API pagination to retrieve all items from an endpoint using a shared session, with per_page sizing."""
    if session is None:
        session = requests.Session()
    # Append per_page if not already present
    if "per_page=" not in url:
        url += ("&" if "?" in url else "?") + f"per_page={per_page}"
    items, next_url = [], url
    while next_url:
        try:
            response = session.get(next_url, headers=headers, timeout=timeout)
            response.raise_for_status()
            items.extend(response.json())
            next_url = None
            if "Link" in response.headers:
                links = requests.utils.parse_header_links(response.headers["Link"])
                next_url = next(
                    (link["url"] for link in links if link.get("rel") == "next"), None
                )
        except requests.exceptions.RequestException as e:
            if not suppress_errors:
                print(f"Error fetching data from Canvas: {e}")
            break
    return items


def download_canvas_file(
    file_url, local_path, headers, session: Optional[requests.Session], timeout: int
):
    """Downloads a file from a Canvas URL to a local path."""
    if session is None:
        session = requests.Session()
    try:
        with session.get(file_url, headers=headers, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return True
    except requests.exceptions.RequestException as e:
        print(f"Failed to download {file_url}: {e}")
        return False


# --- Main Sync Logic ---


def process_canvas_file(
    file_info,
    folder_path_or_id,
    processed_canvas_file_ids,
    canvas_headers,
    storage_type,
    drive_service=None,
    local_root_dir=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    drive_chunk_size_mb: int = DEFAULT_DRIVE_CHUNK_SIZE_MB,
    summary: Optional[SummaryCollector] = None,
    course_name: Optional[str] = None,
    dest_label: Optional[str] = None,
):
    """Helper function to check, download, and save/upload a single Canvas file."""
    file_id = file_info.get("id")
    filename = file_info.get("display_name")
    file_download_url = file_info.get("url")
    file_size = file_info.get("size")
    file_updated_at = file_info.get("updated_at")

    if (
        not all([file_id, filename, file_download_url])
        or file_id in processed_canvas_file_ids
    ):
        return 0

    processed_canvas_file_ids.add(file_id)

    # Get existing file metadata
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            folder_path_or_id, filename
        )

    # Check if file has changed
    if not has_file_changed(
        existing_metadata, canvas_size=file_size, canvas_updated_at=file_updated_at
    ):
        return 0  # No change

    print(f"{'Updating' if existing_metadata else 'New'} file found: '{filename}'")
    local_filepath = os.path.join(DOWNLOAD_DIR, filename)
    if download_canvas_file(
        file_download_url, local_filepath, canvas_headers, session, timeout
    ):
        existing_file_id = existing_metadata.get("id") if existing_metadata else None
        if storage_type == "google_drive":
            success = upload_file_to_drive(
                drive_service,
                local_filepath,
                filename,
                folder_path_or_id,
                existing_file_id,
                drive_chunk_size_mb=drive_chunk_size_mb,
            )
        else:  # local storage
            success = save_file_locally(local_filepath, filename, folder_path_or_id)

        if success:
            # Record in summary
            if summary and course_name and dest_label:
                summary.add_file(
                    course_name,
                    dest_label,
                    filename,
                    "updated" if existing_metadata else "created",
                )
            return 1
        else:
            # If save/upload failed, remove the downloaded file
            if os.path.exists(local_filepath):
                os.remove(local_filepath)
    return 0


def process_canvas_assignment(
    assignment_info,
    assignments_root_path_or_id,
    processed_canvas_file_ids,
    canvas_api_url,
    canvas_headers,
    storage_type,
    drive_service=None,
    local_root_dir=None,
    force_regen_assignments=False,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    drive_chunk_size_mb: int = DEFAULT_DRIVE_CHUNK_SIZE_MB,
    summary: Optional[SummaryCollector] = None,
    course_name: Optional[str] = None,
):
    """Saves an assignment's details and linked files."""
    if session is None:
        session = requests.Session()
    new_items_count = 0
    assignment_name = assignment_info.get("name")
    description = assignment_info.get("description")
    due_at = assignment_info.get("due_at")
    points_possible = assignment_info.get("points_possible")
    rubric = assignment_info.get("rubric") or assignment_info.get("rubric_settings")
    updated_at = assignment_info.get("updated_at")

    if not assignment_name:
        return 0

    safe_assignment_name = sanitize_filename(assignment_name)
    assignment_folder_name = safe_assignment_name

    # Create a dedicated subfolder for the assignment
    if storage_type == "google_drive":
        assignment_storage_path = get_or_create_folder(
            drive_service,
            assignment_folder_name,
            parent_id=assignments_root_path_or_id,
        )
        if not assignment_storage_path:
            return 0
        pdf_filename = f"{safe_assignment_name}.pdf"
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, assignment_storage_path, pdf_filename
        )
    else:  # local storage
        assignment_storage_path = get_or_create_local_folder(
            assignments_root_path_or_id, assignment_folder_name
        )
        pdf_filename = f"{safe_assignment_name}.pdf"
        existing_metadata = get_existing_file_metadata_local(
            assignment_storage_path, pdf_filename
        )

    # Check if assignment has changed (or force regeneration via config)
    if not force_regen_assignments and not has_file_changed(
        existing_metadata, canvas_updated_at=updated_at
    ):
        # Still need to process linked files, but skip PDF generation
        pass
    else:
        print(
            f"{'Updating' if existing_metadata else 'New'} assignment found: '{assignment_name}'"
        )
        local_pdf_path = os.path.join(DOWNLOAD_DIR, pdf_filename)
        try:
            # Create PDF document
            doc = SimpleDocTemplate(local_pdf_path, pagesize=letter)
            styles = getSampleStyleSheet()

            # Create custom styles
            title_style = ParagraphStyle(
                "CustomTitle",
                parent=styles["Heading1"],
                fontSize=16,
                spaceAfter=30,
            )
            normal_style = styles["Normal"]
            bold_style = styles["Normal"]
            bold_style.fontName = "Helvetica-Bold"

            # Build PDF content
            content = []

            # Title
            escaped_assignment_name = html.escape(assignment_name, quote=False)
            content.append(Paragraph(escaped_assignment_name, title_style))
            content.append(Spacer(1, 12))

            # Due date
            if due_at:
                escaped_due_at = html.escape(str(due_at), quote=False)
                content.append(Paragraph(f"<b>Due:</b> {escaped_due_at}", normal_style))
            else:
                content.append(Paragraph("<b>Due:</b> N/A", normal_style))
            content.append(Spacer(1, 6))

            # Points
            if points_possible:
                escaped_points = html.escape(str(points_possible), quote=False)
                content.append(
                    Paragraph(f"<b>Points:</b> {escaped_points}", normal_style)
                )
            else:
                content.append(Paragraph("<b>Points:</b> N/A", normal_style))
            content.append(Spacer(1, 12))

            # Rubric
            if rubric and len(rubric) > 0:
                content.append(Paragraph("<b>Rubric:</b>", bold_style))
                content.append(Spacer(1, 6))

                try:
                    for criterion in rubric:
                        if isinstance(criterion, dict):
                            criterion_desc = criterion.get("description", "")
                            criterion_long_desc = criterion.get("long_description", "")
                            criterion_points = criterion.get("points", 0)

                            if criterion_desc:
                                escaped_criterion_desc = html.escape(
                                    criterion_desc, quote=False
                                )
                                criterion_text = f"<b>{escaped_criterion_desc}</b> ({criterion_points} points)"
                                content.append(Paragraph(criterion_text, normal_style))

                                # Add criterion long description if available
                                if (
                                    criterion_long_desc
                                    and criterion_long_desc.strip()
                                    and criterion_long_desc != criterion_desc
                                ):
                                    # Process HTML content properly with safe fallback
                                    html_elements = html_to_pdf_elements(
                                        f"<i>{criterion_long_desc}</i>", styles
                                    )
                                    if html_elements:
                                        content.extend(html_elements)
                                    else:
                                        # Fallback to plain text if inline-only content produced nothing
                                        try:
                                            from bs4 import BeautifulSoup as _BS

                                            plain = _BS(
                                                criterion_long_desc, "html.parser"
                                            ).get_text(" ", strip=True)
                                        except Exception:
                                            plain = criterion_long_desc
                                        if plain and plain.strip():
                                            content.append(
                                                Paragraph(
                                                    f"<i>{html.escape(plain, quote=False)}</i>",
                                                    normal_style,
                                                )
                                            )
                                    content.append(Spacer(1, 3))
                                else:
                                    content.append(Spacer(1, 3))

                            # Add ratings if available
                            ratings = criterion.get("ratings", [])
                            if ratings and isinstance(ratings, list):
                                for rating in ratings:
                                    if isinstance(rating, dict):
                                        rating_desc = rating.get("description", "")
                                        rating_long_desc = rating.get(
                                            "long_description", ""
                                        )
                                        rating_small_desc = rating.get(
                                            "small_description", ""
                                        )
                                        rating_points = rating.get("points", 0)

                                        if rating_desc:
                                            escaped_rating_desc = html.escape(
                                                rating_desc, quote=False
                                            )
                                            rating_text = f"  • {escaped_rating_desc} ({rating_points} points)"
                                            content.append(
                                                Paragraph(rating_text, normal_style)
                                            )

                                            # Add long description if available and different from main description
                                            if (
                                                rating_long_desc
                                                and rating_long_desc.strip()
                                                and rating_long_desc != rating_desc
                                            ):
                                                # Process HTML content properly with safe fallback
                                                html_elements = html_to_pdf_elements(
                                                    f"    <i>{rating_long_desc}</i>",
                                                    styles,
                                                )
                                                if html_elements:
                                                    content.extend(html_elements)
                                                else:
                                                    try:
                                                        from bs4 import (
                                                            BeautifulSoup as _BS,
                                                        )

                                                        plain = _BS(
                                                            rating_long_desc,
                                                            "html.parser",
                                                        ).get_text(" ", strip=True)
                                                    except Exception:
                                                        plain = rating_long_desc
                                                    if plain and plain.strip():
                                                        content.append(
                                                            Paragraph(
                                                                f"<i>{html.escape(plain, quote=False)}</i>",
                                                                normal_style,
                                                            )
                                                        )
                                                content.append(Spacer(1, 2))

                                            # Add small description if available and different
                                            elif (
                                                rating_small_desc
                                                and rating_small_desc.strip()
                                                and rating_small_desc != rating_desc
                                            ):
                                                # Process HTML content properly with safe fallback
                                                html_elements = html_to_pdf_elements(
                                                    f"    <i>{rating_small_desc}</i>",
                                                    styles,
                                                )
                                                if html_elements:
                                                    content.extend(html_elements)
                                                else:
                                                    try:
                                                        from bs4 import (
                                                            BeautifulSoup as _BS,
                                                        )

                                                        plain = _BS(
                                                            rating_small_desc,
                                                            "html.parser",
                                                        ).get_text(" ", strip=True)
                                                    except Exception:
                                                        plain = rating_small_desc
                                                    if plain and plain.strip():
                                                        content.append(
                                                            Paragraph(
                                                                f"<i>{html.escape(plain, quote=False)}</i>",
                                                                normal_style,
                                                            )
                                                        )
                                                content.append(Spacer(1, 2))
                                content.append(Spacer(1, 6))
                    content.append(Spacer(1, 12))
                except Exception as e:
                    escaped_error = html.escape(str(e), quote=False)
                    content.append(
                        Paragraph(
                            f"<i>Error processing rubric: {escaped_error}</i>",
                            normal_style,
                        )
                    )
                    content.append(Spacer(1, 12))

            # Separator
            content.append(Paragraph("<hr/>", normal_style))
            content.append(Spacer(1, 12))

            # Description
            if description:
                # Convert HTML to formatted PDF elements
                html_elements = html_to_pdf_elements(description, styles)
                content.extend(html_elements)

            # Generate PDF
            doc.build(content)

            existing_file_id = (
                existing_metadata.get("id") if existing_metadata else None
            )
            if storage_type == "google_drive":
                success = upload_file_to_drive(
                    drive_service,
                    local_pdf_path,
                    pdf_filename,
                    assignment_storage_path,
                    existing_file_id,
                )
            else:
                success = save_file_locally(
                    local_pdf_path,
                    pdf_filename,
                    assignment_storage_path,
                )
            if success:
                new_items_count += 1
                # Record in summary
                if summary and course_name:
                    dest_label = f"{course_name}/Assignments/{assignment_folder_name}"
                    summary.add_file(
                        course_name,
                        dest_label,
                        pdf_filename,
                        "updated" if existing_metadata else "created",
                    )
            # Clean up temporary PDF file
            if os.path.exists(local_pdf_path):
                try:
                    os.remove(local_pdf_path)
                except OSError as e:
                    print(
                        f"Warning: Could not remove temporary file '{local_pdf_path}': {e}"
                    )
        except Exception as e:
            escaped_error = html.escape(str(e), quote=False)
            print(
                f"Could not save assignment '{assignment_name}' as PDF: {escaped_error}"
            )
            # Clean up temporary PDF file if it exists
            if os.path.exists(local_pdf_path):
                try:
                    os.remove(local_pdf_path)
                except OSError as e:
                    print(
                        f"Warning: Could not remove temporary file '{local_pdf_path}': {e}"
                    )

    # Avoid listing entire folder contents to reduce API calls; rely on per-file metadata checks.

    # Scan the assignment description for linked files
    if description:
        soup = BeautifulSoup(description, "html.parser")
        for link in soup.find_all("a", href=True):
            if not isinstance(link, Tag):
                continue
            href = link.get("href", "")
            if not isinstance(href, str):
                continue
            match = re.search(r"/files/(\d+)", href)
            if match:
                file_id = match.group(1)
                file_api_url = f"{canvas_api_url}/api/v1/files/{file_id}"
                try:
                    file_info_resp = session.get(
                        file_api_url, headers=canvas_headers, timeout=timeout
                    )
                    file_info_resp.raise_for_status()
                    if file_info_resp.ok:
                        new_items_count += process_canvas_file(
                            file_info_resp.json(),
                            assignment_storage_path,
                            processed_canvas_file_ids,
                            canvas_headers,
                            storage_type,
                            drive_service,
                            local_root_dir,
                            session=session,
                            timeout=timeout,
                            drive_chunk_size_mb=drive_chunk_size_mb,
                            summary=summary,
                            course_name=course_name,
                            dest_label=f"{course_name}/Assignments/{assignment_folder_name}",
                        )
                except requests.RequestException as e:
                    print(f"Could not fetch file link from assignment: {e}")

    return new_items_count


def _parse_iso_utc(dt_str: str):
    """Parse an ISO 8601 string (potentially with trailing 'Z') into a timezone-aware datetime in UTC.

    Returns None if parsing fails or input is falsy.
    """
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        from datetime import datetime, timezone

        ds = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ds)
        # Ensure tz-aware and in UTC
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _to_utc_datetime(value):
    """Best-effort conversion of various timestamp representations to UTC datetime.

    Supports:
    - ISO 8601 strings (with or without 'Z')
    - POSIX timestamps (float/int seconds since epoch)
    Returns None if conversion fails.
    """
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        return _parse_iso_utc(value)
    return None


def _max_iso_datetime(values: List[str]):
    """Return the max ISO timestamp (UTC) from a list of timestamp strings."""
    try:
        from datetime import timezone

        timestamps = [_parse_iso_utc(v) for v in values if v]
        timestamps = [t for t in timestamps if t]
        if not timestamps:
            return None
        return max(timestamps).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _max_timestamp_from_items(items: List[Dict], keys: List[str]):
    """Best-effort extraction of the newest timestamp from a list of dict items."""
    if not items:
        return None
    collected = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if value:
                collected.append(value)
    return _max_iso_datetime(collected)


def _should_regenerate_resource(existing_metadata, newest_iso: Optional[str]):
    """Decide whether to regenerate a derived resource based on newest item timestamp."""
    if not existing_metadata:
        return True
    if newest_iso:
        existing_dt = _to_utc_datetime(existing_metadata.get("modified_time"))
        newest_dt = _parse_iso_utc(newest_iso)
        if existing_dt and newest_dt and newest_dt <= existing_dt:
            return False
    return True


def _export_json_resource(
    data,
    filename: str,
    folder_path_or_id,
    storage_type: str,
    drive_service=None,
    existing_metadata=None,
    summary: Optional[SummaryCollector] = None,
    course_name: Optional[str] = None,
    dest_label: Optional[str] = None,
):
    """Serialize data to JSON, upload/save, record summary, and cleanup temp file."""
    local_json_path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        with open(local_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        existing_file_id = existing_metadata.get("id") if existing_metadata else None
        if storage_type == "google_drive":
            success = upload_file_to_drive(
                drive_service,
                local_json_path,
                filename,
                folder_path_or_id,
                existing_file_id,
            )
        else:
            success = save_file_locally(local_json_path, filename, folder_path_or_id)

        if success and summary and course_name and dest_label:
            summary.add_file(
                course_name,
                dest_label,
                filename,
                "updated" if existing_metadata else "created",
            )
        return 1 if success else 0
    finally:
        if os.path.exists(local_json_path):
            try:
                os.remove(local_json_path)
            except OSError:
                pass


def _get_bool_config(
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: bool,
) -> bool:
    if not config.has_section(section):
        return default
    try:
        value = config.get(section, option, fallback=str(default)).strip().lower()
        return value in {"1", "true", "yes", "y", "on"}
    except Exception:
        return default


def process_course_pages(
    course_id: int,
    course_name: str,
    course_storage_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    """Fetch all course pages, merge them into a single PDF, and upload/save if changed.

    - Creates/uses a "Pages" folder under the course directory.
    - Output filename: "All Pages.pdf".
    - Change detection: compares max(page.updated_at) vs existing PDF modified time.
    """
    if session is None:
        session = requests.Session()

    # Build/get destination folder
    pages_folder_label = f"{course_name}/Pages"
    output_filename = "All Pages.pdf"

    if storage_type == "google_drive":
        pages_folder_path_or_id = get_or_create_folder(
            drive_service, "Pages", parent_id=course_storage_path_or_id
        )
    else:
        pages_folder_path_or_id = get_or_create_local_folder(
            course_storage_path_or_id, "Pages"
        )

    if not pages_folder_path_or_id:
        return 0

    # Fetch pages with body included (normalize base URL to avoid double slashes)
    base_url = (canvas_api_url or "").rstrip("/")
    pages_url = f"{base_url}/api/v1/courses/{course_id}/pages?include[]=body"
    pages_from_api = get_paginated_canvas_items(
        pages_url, canvas_headers, session, timeout, per_page, suppress_errors=True
    )

    # Always also discover pages via modules to catch pages that may not appear in the main pages list
    # (e.g., pages only in modules, unpublished pages, or due to permissions)
    pages_map = {}

    # Add pages from the direct API endpoint first
    for page in pages_from_api or []:
        slug = page.get("url") or page.get("page_url") or page.get("title")
        if slug:
            pages_map[slug] = page

    # Then discover and add pages from modules (won't overwrite existing ones)
    try:
        modules_url = f"{base_url}/api/v1/courses/{course_id}/modules"
        modules = get_paginated_canvas_items(
            modules_url,
            canvas_headers,
            session,
            timeout,
            per_page,
            suppress_errors=True,
        )
        for module in modules or []:
            items_url = f"{base_url}/api/v1/courses/{course_id}/modules/{module.get('id')}/items"
            module_items = get_paginated_canvas_items(
                items_url,
                canvas_headers,
                session,
                timeout,
                per_page,
                suppress_errors=True,
            )
            for item in module_items or []:
                if item.get("type") == "Page" and item.get("url"):
                    # Fetch page details to get body and timestamps
                    try:
                        resp = session.get(
                            item["url"], headers=canvas_headers, timeout=timeout
                        )
                        resp.raise_for_status()
                        pd = resp.json()
                        slug = pd.get("url") or item.get("page_url") or pd.get("title")
                        # Only add if not already present from pages API
                        if slug and slug not in pages_map:
                            pages_map[slug] = {
                                "title": pd.get("title"),
                                "body": pd.get("body"),
                                "updated_at": pd.get("updated_at"),
                                "html_url": pd.get("html_url"),
                                "url": pd.get("url"),
                            }
                    except requests.RequestException:
                        continue
    except Exception:
        # If module discovery fails, continue with just the pages from API
        pass

    # Convert map to list
    pages = list(pages_map.values())

    if not pages:
        return 0

    # Sort pages for a stable order (by title)
    try:
        pages.sort(key=lambda p: (p.get("title") or "").lower())
    except Exception:
        pass

    # Determine if anything changed by checking the newest updated_at across pages
    max_updated_at_iso = None
    try:
        page_times = [p.get("updated_at") for p in pages if p.get("updated_at")]
        if page_times:
            # Convert to datetime then back to ISO for consistent compare usage
            from datetime import timezone

            dts = [_parse_iso_utc(t) for t in page_times]
            dts = [dt for dt in dts if dt is not None]
            if dts:
                max_dt = max(dts)
                max_updated_at_iso = max_dt.astimezone(timezone.utc).isoformat()
    except Exception:
        # If we can't compute max time, fallback to regenerating (safer)
        max_updated_at_iso = None

    # Existing metadata lookup
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, pages_folder_path_or_id, output_filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            pages_folder_path_or_id, output_filename
        )

    # Decide whether to rebuild
    should_rebuild = True
    if existing_metadata and max_updated_at_iso:
        # Compare existing modified time (could be iso string for Drive or epoch for local)
        existing_mod = existing_metadata.get("modified_time")
        existing_dt = _to_utc_datetime(existing_mod)
        newest_page_dt = _parse_iso_utc(max_updated_at_iso)
        if existing_dt and newest_page_dt and newest_page_dt <= existing_dt:
            should_rebuild = False

    if not should_rebuild:
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} course pages bundle for '{course_name}'"
    )

    # Create PDF
    local_pdf_path = os.path.join(DOWNLOAD_DIR, output_filename)
    try:
        # Custom DocTemplate to capture headings for TOC and create bookmarks
        class TOCDocTemplate(SimpleDocTemplate):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._h_seq = 0

            def afterFlowable(self, flowable):
                # Capture PageTitle paragraphs as TOC entries and bookmarks
                try:
                    if (
                        isinstance(flowable, Paragraph)
                        and getattr(flowable.style, "name", "") == "PageTitle"
                    ):
                        text = flowable.getPlainText()
                        self._h_seq += 1
                        key = f"h{self._h_seq}"
                        # Bookmark destination on current page
                        self.canv.bookmarkPage(key)
                        # Notify TOC with clickable destination key
                        self.notify("TOCEntry", (0, text, self.page, key))
                except Exception:
                    # Don't block PDF build if TOC capture fails
                    pass

        doc = TOCDocTemplate(local_pdf_path, pagesize=letter)
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "CoursePagesTitle", parent=styles["Heading1"], fontSize=18, spaceAfter=24
        )
        page_title_style = ParagraphStyle(
            "PageTitle", parent=styles["Heading2"], fontSize=14, spaceAfter=12
        )
        toc_title_style = ParagraphStyle(
            "TOCTitle", parent=styles["Heading2"], fontSize=14, spaceAfter=6
        )
        link_style = ParagraphStyle(
            "PageLink",
            parent=styles["Normal"],
            textColor=blue,
            underline=True,
            spaceAfter=6,
        )

        content = []
        # Top title
        content.append(Paragraph(html.escape(f"{course_name} — Pages"), title_style))
        content.append(Spacer(1, 12))

        # Table of Contents section (simple internal links, no page numbers)
        content.append(Paragraph("Table of Contents", toc_title_style))
        content.append(Spacer(1, 4))
        for i, p in enumerate(pages, start=1):
            t = html.escape(p.get("title") or "Untitled Page", quote=False)
            content.append(Paragraph(f'<link href="#h{i}">{t}</link>', link_style))
        content.append(PageBreak())

        # Add each page
        for idx, page in enumerate(pages):
            title = page.get("title") or "Untitled Page"
            body = page.get("body") or ""
            page_url = page.get("html_url")
            if not page_url:
                # Fallback to construct from slug if available
                slug = page.get("url")
                try:
                    parsed = urlparse(canvas_api_url)
                    if parsed.scheme and parsed.netloc and slug:
                        page_url = f"{parsed.scheme}://{parsed.netloc}/courses/{course_id}/pages/{slug}"
                except Exception:
                    page_url = None

            safe_title = html.escape(title, quote=False)
            content.append(Paragraph(safe_title, page_title_style))
            content.append(Spacer(1, 6))

            # External link back to Canvas page (if resolvable)
            if page_url:
                safe_url = html.escape(page_url, quote=True)
                content.append(
                    Paragraph(
                        f'<link href="{safe_url}">View on Canvas</link>', link_style
                    )
                )

            if body:
                html_elements = html_to_pdf_elements(body, styles)
                if html_elements:
                    content.extend(html_elements)
            # Add a page break between pages, except after the last one
            if idx < len(pages) - 1:
                content.append(PageBreak())

        # Build PDF (single pass; internal links don't need page numbers)
        doc.build(content)

        # Upload/Save
        if storage_type == "google_drive":
            existing_file_id = (
                existing_metadata.get("id") if existing_metadata else None
            )
            success = upload_file_to_drive(
                drive_service,
                local_pdf_path,
                output_filename,
                pages_folder_path_or_id,
                existing_file_id,
            )
        else:
            success = save_file_locally(
                local_pdf_path, output_filename, pages_folder_path_or_id
            )

        # Cleanup temp
        if os.path.exists(local_pdf_path):
            try:
                os.remove(local_pdf_path)
            except OSError:
                pass

        if success:
            # Record summary
            if summary is not None:
                summary.add_file(
                    course_name,
                    pages_folder_label,
                    output_filename,
                    "updated" if existing_metadata else "created",
                )
            return 1
    except Exception as e:
        print(f"Failed to build/upload merged pages PDF for '{course_name}': {e}")
        # Cleanup temp if present
        if os.path.exists(local_pdf_path):
            try:
                os.remove(local_pdf_path)
            except OSError:
                pass

    return 0


def process_course_announcements(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    announcements_url = (
        f"{base_url}/api/v1/announcements?context_codes[]=course_{course_id}"
    )
    announcements = get_paginated_canvas_items(
        announcements_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not announcements:
        return 0

    latest_ts = _max_timestamp_from_items(
        announcements, ["posted_at", "last_reply_at", "updated_at"]
    )
    filename = "announcements.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} announcements for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        announcements,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_discussions(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    discussions_url = f"{base_url}/api/v1/courses/{course_id}/discussion_topics"
    discussions = get_paginated_canvas_items(
        discussions_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not discussions:
        return 0

    latest_ts = _max_timestamp_from_items(
        discussions, ["last_reply_at", "posted_at", "updated_at"]
    )
    filename = "discussion_topics.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} discussions for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        discussions,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_quizzes(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    quizzes_url = f"{base_url}/api/v1/courses/{course_id}/quizzes"
    quizzes = get_paginated_canvas_items(
        quizzes_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not quizzes:
        return 0

    latest_ts = _max_timestamp_from_items(quizzes, ["updated_at", "published_at"])
    filename = "quizzes.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(f"{'Updating' if existing_metadata else 'New'} quizzes for '{course_name}'")
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        quizzes,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_enrollments(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    enrollments_url = f"{base_url}/api/v1/courses/{course_id}/enrollments"
    enrollments = get_paginated_canvas_items(
        enrollments_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not enrollments:
        return 0

    latest_ts = _max_timestamp_from_items(
        enrollments, ["updated_at", "last_activity_at", "created_at"]
    )
    filename = "enrollments.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} enrollments for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        enrollments,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_calendar_events(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    calendar_url = (
        f"{base_url}/api/v1/calendar_events?context_codes[]=course_{course_id}"
    )
    calendar_events = get_paginated_canvas_items(
        calendar_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not calendar_events:
        return 0

    latest_ts = _max_timestamp_from_items(
        calendar_events, ["updated_at", "start_at", "end_at"]
    )
    filename = "calendar_events.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} calendar events for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        calendar_events,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_groups(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    groups_url = f"{base_url}/api/v1/courses/{course_id}/groups"
    groups = get_paginated_canvas_items(
        groups_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not groups:
        return 0

    latest_ts = _max_timestamp_from_items(groups, ["updated_at", "created_at"])
    filename = "groups.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(f"{'Updating' if existing_metadata else 'New'} groups for '{course_name}'")
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        groups,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_analytics_activity(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    analytics_url = f"{base_url}/api/v1/courses/{course_id}/analytics/activity"
    try:
        resp = session.get(analytics_url, headers=canvas_headers, timeout=timeout)
        resp.raise_for_status()
        analytics_payload = resp.json()
    except requests.RequestException as e:
        print(f"Could not fetch analytics for course {course_id}: {e}")
        return 0

    if not analytics_payload:
        return 0

    combined_items: List[Dict] = []
    if isinstance(analytics_payload, list):
        combined_items = [i for i in analytics_payload if isinstance(i, dict)]
    elif isinstance(analytics_payload, dict):
        for value in analytics_payload.values():
            if isinstance(value, list):
                combined_items.extend([i for i in value if isinstance(i, dict)])

    latest_ts = _max_timestamp_from_items(
        combined_items, ["created_at", "updated_at", "last_activity_at"]
    )
    filename = "analytics_activity.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} analytics activity for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        analytics_payload,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_gradebook_history(
    course_id: int,
    course_name: str,
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    history_url = f"{base_url}/api/v1/courses/{course_id}/gradebook_history/feed"
    try:
        resp = session.get(history_url, headers=canvas_headers, timeout=timeout)
        resp.raise_for_status()
        history_payload = resp.json()
    except requests.RequestException as e:
        print(f"Could not fetch gradebook history for course {course_id}: {e}")
        return 0

    if not history_payload:
        return 0

    latest_ts = _max_timestamp_from_items(
        history_payload, ["graded_at", "posted_at", "created_at", "updated_at"]
    )
    filename = "gradebook_history.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} gradebook history for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        history_payload,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_course_submissions_summary(
    course_id: int,
    course_name: str,
    assignments: List[Dict],
    reports_folder_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    if session is None:
        session = requests.Session()

    if not assignments:
        return 0

    base_url = (canvas_api_url or "").rstrip("/")
    collected_submissions = []

    for assignment in assignments:
        assignment_id = assignment.get("id")
        if not assignment_id:
            continue
        assignment_name = assignment.get("name")
        submissions_url = f"{base_url}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions"
        submissions = get_paginated_canvas_items(
            submissions_url,
            canvas_headers,
            session,
            timeout,
            per_page,
            suppress_errors=True,
        )
        for submission in submissions or []:
            if not isinstance(submission, dict):
                continue
            collected_submissions.append(
                {
                    "assignment_id": assignment_id,
                    "assignment_name": assignment_name,
                    "id": submission.get("id"),
                    "user_id": submission.get("user_id"),
                    "submitted_at": submission.get("submitted_at"),
                    "graded_at": submission.get("graded_at"),
                    "posted_at": submission.get("posted_at"),
                    "workflow_state": submission.get("workflow_state"),
                    "score": submission.get("score"),
                    "grade": submission.get("grade"),
                    "attempt": submission.get("attempt"),
                }
            )

    if not collected_submissions:
        return 0

    latest_ts = _max_timestamp_from_items(
        collected_submissions, ["graded_at", "posted_at", "submitted_at"]
    )
    filename = "submissions_summary.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, reports_folder_path_or_id, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            reports_folder_path_or_id, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(
        f"{'Updating' if existing_metadata else 'New'} submissions summary for '{course_name}'"
    )
    reports_label = f"{course_name}/Reports"
    return _export_json_resource(
        collected_submissions,
        filename,
        reports_folder_path_or_id,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        course_name,
        reports_label,
    )


def process_inbox_conversations(
    root_storage_path_or_id,
    canvas_api_url: str,
    canvas_headers: dict,
    storage_type: str,
    drive_service=None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    per_page: int = DEFAULT_CANVAS_PER_PAGE,
    summary: Optional[SummaryCollector] = None,
):
    """Fetch user inbox conversations (global, not course-specific)."""
    if session is None:
        session = requests.Session()

    base_url = (canvas_api_url or "").rstrip("/")
    conversations_url = f"{base_url}/api/v1/conversations"
    conversations = get_paginated_canvas_items(
        conversations_url,
        canvas_headers,
        session,
        timeout,
        per_page,
        suppress_errors=True,
    )
    if not conversations:
        return 0

    latest_ts = _max_timestamp_from_items(
        conversations, ["last_message_at", "updated_at"]
    )
    if storage_type == "google_drive":
        conversations_folder = get_or_create_folder(
            drive_service, "Conversations", parent_id=root_storage_path_or_id
        )
    else:
        conversations_folder = get_or_create_local_folder(
            root_storage_path_or_id, "Conversations"
        )

    if not conversations_folder:
        return 0

    filename = "conversations.json"
    if storage_type == "google_drive":
        existing_metadata = get_existing_file_metadata_drive(
            drive_service, conversations_folder, filename
        )
    else:
        existing_metadata = get_existing_file_metadata_local(
            conversations_folder, filename
        )

    if not _should_regenerate_resource(existing_metadata, latest_ts):
        return 0

    print(f"{'Updating' if existing_metadata else 'New'} inbox conversations archive")
    return _export_json_resource(
        conversations,
        filename,
        conversations_folder,
        storage_type,
        drive_service,
        existing_metadata,
        summary,
        "Inbox",
        "Inbox/Conversations",
    )


def main():
    """Main function to run the sync process."""
    print("--- Starting Canvas to Storage Sync ---")
    summary = SummaryCollector()

    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: Config file '{CONFIG_FILE}' not found.")
        return
    config.read(CONFIG_FILE)

    try:
        canvas_api_url = config["CANVAS"]["API_URL"]
        canvas_api_key = config["CANVAS"]["API_KEY"]
        storage_type = config["STORAGE"]["STORAGE_TYPE"].lower()
        # Optional: force regenerate assignment PDFs regardless of Canvas updated_at
        force_regen_assignments = config["STORAGE"].get(
            "FORCE_REGENERATE_ASSIGNMENTS", "false"
        ).strip().lower() in {"1", "true", "yes", "y", "on"}

        local_root_dir: Optional[str] = None
        drive_root_folder_name: Optional[str] = None
        if storage_type == "google_drive":
            drive_root_folder_name = config["STORAGE"]["ROOT_FOLDER_NAME"]
        elif storage_type == "local":
            local_root_dir = config["STORAGE"]["LOCAL_ROOT_DIR"]
        else:
            print(
                f"ERROR: Invalid STORAGE_TYPE '{storage_type}'. Must be 'local' or 'google_drive'"
            )
            return
    except KeyError as e:
        print(f"ERROR: Missing config key in {CONFIG_FILE}: {e}")
        return

    canvas_headers = {"Authorization": f"Bearer {canvas_api_key}"}

    # Initialize storage service
    drive_service = None
    if storage_type == "google_drive":
        drive_service = get_drive_service()
        if not drive_service:
            return
        root_storage_path = get_or_create_folder(drive_service, drive_root_folder_name)
        if not root_storage_path:
            return
        print(f"Syncing to Google Drive folder: '{drive_root_folder_name}'")
    else:  # local storage
        if local_root_dir is None:
            print("ERROR: LOCAL_ROOT_DIR not configured.")
            return
        root_storage_path = os.path.abspath(local_root_dir)
        if not os.path.exists(root_storage_path):
            os.makedirs(root_storage_path)
        print(f"Syncing to local directory: '{root_storage_path}'")

    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR)

    # Performance tuning from config (optional)
    try:
        perf_cfg = config["PERFORMANCE"] if config.has_section("PERFORMANCE") else {}
        request_timeout = int(perf_cfg.get("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))
        max_retries = int(perf_cfg.get("MAX_RETRIES", DEFAULT_MAX_RETRIES))
        backoff_factor = float(perf_cfg.get("BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR))
        canvas_per_page = int(perf_cfg.get("CANVAS_PER_PAGE", DEFAULT_CANVAS_PER_PAGE))
        http_pool_maxsize = int(
            perf_cfg.get("HTTP_POOL_MAXSIZE", DEFAULT_HTTP_POOL_MAXSIZE)
        )
        drive_chunk_size_mb = int(
            perf_cfg.get("DRIVE_CHUNK_SIZE_MB", DEFAULT_DRIVE_CHUNK_SIZE_MB)
        )
    except Exception:
        request_timeout = DEFAULT_REQUEST_TIMEOUT
        max_retries = DEFAULT_MAX_RETRIES
        backoff_factor = DEFAULT_BACKOFF_FACTOR
        canvas_per_page = DEFAULT_CANVAS_PER_PAGE
        http_pool_maxsize = DEFAULT_HTTP_POOL_MAXSIZE
        drive_chunk_size_mb = DEFAULT_DRIVE_CHUNK_SIZE_MB

    # Export toggles
    export_announcements = _get_bool_config(
        config, "EXPORTS", "EXPORT_ANNOUNCEMENTS", True
    )
    export_discussions = _get_bool_config(config, "EXPORTS", "EXPORT_DISCUSSIONS", True)
    export_quizzes = _get_bool_config(config, "EXPORTS", "EXPORT_QUIZZES", True)
    export_enrollments = _get_bool_config(config, "EXPORTS", "EXPORT_ENROLLMENTS", True)
    export_calendar_events = _get_bool_config(
        config, "EXPORTS", "EXPORT_CALENDAR_EVENTS", True
    )
    export_groups = _get_bool_config(config, "EXPORTS", "EXPORT_GROUPS", True)
    export_analytics = _get_bool_config(
        config, "EXPORTS", "EXPORT_ANALYTICS_ACTIVITY", True
    )
    export_gradebook = _get_bool_config(
        config, "EXPORTS", "EXPORT_GRADEBOOK_HISTORY", True
    )
    export_submissions = _get_bool_config(
        config, "EXPORTS", "EXPORT_SUBMISSIONS_SUMMARY", False
    )
    export_inbox = _get_bool_config(
        config, "EXPORTS", "EXPORT_INBOX_CONVERSATIONS", False
    )

    # Shared HTTP session with retries and connection pooling

    session = requests.Session()
    retries = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT", "PATCH"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=http_pool_maxsize,
        pool_maxsize=http_pool_maxsize,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    print("\nFetching courses from Canvas...")
    courses_url = f"{canvas_api_url}/api/v1/courses"
    courses = get_paginated_canvas_items(
        courses_url, canvas_headers, session, request_timeout, canvas_per_page
    )
    if not courses:
        print("No courses found.")
        return

    # Filter out restricted courses
    available_courses = [
        course
        for course in courses
        if course.get("id") and not course.get("access_restricted_by_date")
    ]

    if not available_courses:
        print("No available courses found (all may be restricted).")
        return

    # Load last selection
    last_course_ids = load_last_selection()

    # Get user selection
    selected_courses = display_courses_and_get_selection(
        available_courses, last_course_ids
    )

    if not selected_courses:
        print("No courses selected. Exiting.")
        return

    # Save the selection for next time
    save_last_selection(selected_courses)

    print(f"\nSelected {len(selected_courses)} course(s) to sync.")

    for course in selected_courses:

        course_name, course_id = course.get("name", "Unnamed"), course.get("id")

        print(f"\n--- Processing Course: {course_name} ---")

        # --- Process Quizzes ---
        if export_quizzes:
            print("Searching for quizzes...")
            quizzes = get_canvas_quizzes(
                course_id,
                session,
                canvas_api_url,
                canvas_api_key,
                timeout=request_timeout,
                per_page=canvas_per_page,
            )
            if quizzes:
                print(f"Found {len(quizzes)} quizzes in '{course_name}':")
                for quiz in quizzes:
                    title = quiz.get("title", "(untitled)")
                    due_at = quiz.get("due_at", "N/A")
                    points = quiz.get("points_possible", "N/A")
                    print(f"  - {title} | Due: {due_at} | Points: {points}")
            else:
                print("No quizzes found.")

        if storage_type == "google_drive":
            course_storage_path = get_or_create_folder(
                drive_service, course_name, parent_id=root_storage_path
            )
            if not course_storage_path:
                continue
        else:  # local storage
            course_storage_path = get_or_create_local_folder(
                local_root_dir, course_name
            )

        # Prepare per-course reports folder for aggregated exports
        if storage_type == "google_drive":
            reports_folder_path = get_or_create_folder(
                drive_service, "Reports", parent_id=course_storage_path
            )
        else:
            reports_folder_path = get_or_create_local_folder(
                course_storage_path, "Reports"
            )

        processed_canvas_file_ids = set()
        new_items_synced = 0

        # --- Process Assignments ---
        print("Searching for assignments...")
        assignments_url = (
            f"{canvas_api_url}/api/v1/courses/{course_id}/assignments?include[]=rubric"
        )
        assignments = get_paginated_canvas_items(
            assignments_url, canvas_headers, session, request_timeout, canvas_per_page
        )
        if assignments:
            if storage_type == "google_drive":
                assignments_folder_path = get_or_create_folder(
                    drive_service, "Assignments", parent_id=course_storage_path
                )
            else:
                assignments_folder_path = get_or_create_local_folder(
                    course_storage_path, "Assignments"
                )

            if assignments_folder_path:
                for assignment in assignments:
                    new_items_synced += process_canvas_assignment(
                        assignment,
                        assignments_folder_path,
                        processed_canvas_file_ids,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        local_root_dir,
                        force_regen_assignments=force_regen_assignments,
                        session=session,
                        timeout=request_timeout,
                        drive_chunk_size_mb=drive_chunk_size_mb,
                        summary=summary,
                        course_name=course_name,
                    )

        # --- Process Modules (Files and Pages) ---
        print("Searching for files and pages in modules...")
        modules_url = f"{canvas_api_url}/api/v1/courses/{course_id}/modules"
        modules = get_paginated_canvas_items(
            modules_url, canvas_headers, session, request_timeout, canvas_per_page
        )

        for module in modules:
            items_url = f"{canvas_api_url}/api/v1/courses/{course_id}/modules/{module['id']}/items"
            module_items = get_paginated_canvas_items(
                items_url, canvas_headers, session, request_timeout, canvas_per_page
            )

            for item in module_items:
                try:
                    # Case 1: Item is a direct file link
                    if item.get("type") == "File":
                        file_details_resp = session.get(
                            item["url"], headers=canvas_headers, timeout=request_timeout
                        )
                        file_details_resp.raise_for_status()
                        new_items_synced += process_canvas_file(
                            file_details_resp.json(),
                            course_storage_path,
                            processed_canvas_file_ids,
                            canvas_headers,
                            storage_type,
                            drive_service,
                            local_root_dir,
                            session=session,
                            timeout=request_timeout,
                            drive_chunk_size_mb=drive_chunk_size_mb,
                            summary=summary,
                            course_name=course_name,
                            dest_label=f"{course_name}",
                        )

                    # Case 2: Item is a Page, which we save as an HTML file
                    elif item.get("type") == "Page":
                        page_resp = session.get(
                            item["url"], headers=canvas_headers, timeout=request_timeout
                        )
                        page_resp.raise_for_status()
                        page_data = page_resp.json()
                        page_title = page_data.get("title")
                        html_body = page_data.get("body")

                        if not page_title or not html_body:
                            continue

                        safe_page_title = sanitize_filename(page_title)
                        page_folder_name = safe_page_title

                        if storage_type == "google_drive":
                            page_storage_path = get_or_create_folder(
                                drive_service,
                                page_folder_name,
                                parent_id=course_storage_path,
                            )
                            if not page_storage_path:
                                continue
                            # Avoid full folder listing for performance
                        else:  # local storage
                            page_storage_path = get_or_create_local_folder(
                                course_storage_path, page_folder_name
                            )

                        pdf_filename = f"{safe_page_title}.pdf"
                        updated_at = page_data.get("updated_at")

                        # Get existing PDF metadata
                        if storage_type == "google_drive":
                            existing_metadata = get_existing_file_metadata_drive(
                                drive_service, page_storage_path, pdf_filename
                            )
                        else:
                            existing_metadata = get_existing_file_metadata_local(
                                page_storage_path, pdf_filename
                            )

                        # Check if page has changed
                        if not has_file_changed(
                            existing_metadata, canvas_updated_at=updated_at
                        ):
                            # Skip PDF generation, but still process linked files
                            pass
                        else:
                            # Create the full HTML content for both HTML and PDF generation
                            full_html = f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{page_title}</title></head><body>{html_body}</body></html>'

                            print(
                                f"{'Updating' if existing_metadata else 'New'} page found: '{page_title}'"
                            )
                            local_pdf_path = os.path.join(DOWNLOAD_DIR, pdf_filename)
                            try:
                                # Create PDF document
                                doc = SimpleDocTemplate(local_pdf_path, pagesize=letter)
                                styles = getSampleStyleSheet()

                                # Create title style
                                title_style = ParagraphStyle(
                                    "CustomTitle",
                                    parent=styles["Heading1"],
                                    fontSize=16,
                                    spaceAfter=30,
                                )

                                story = []

                                # Add title
                                escaped_page_title = html.escape(
                                    page_title, quote=False
                                )
                                story.append(Paragraph(escaped_page_title, title_style))
                                story.append(Spacer(1, 12))

                                # Add content with preserved formatting
                                html_elements = html_to_pdf_elements(full_html, styles)
                                story.extend(html_elements)

                                doc.build(story)

                                existing_file_id = (
                                    existing_metadata.get("id")
                                    if existing_metadata
                                    else None
                                )
                                if storage_type == "google_drive":
                                    success = upload_file_to_drive(
                                        drive_service,
                                        local_pdf_path,
                                        pdf_filename,
                                        page_storage_path,
                                        existing_file_id,
                                    )
                                else:
                                    success = save_file_locally(
                                        local_pdf_path,
                                        pdf_filename,
                                        page_storage_path,
                                    )
                                if success:
                                    new_items_synced += 1
                                    # Record in summary
                                    dest_label = f"{course_name}/{page_folder_name}"
                                    summary.add_file(
                                        course_name,
                                        dest_label,
                                        pdf_filename,
                                        "updated" if existing_metadata else "created",
                                    )
                                # Clean up temporary PDF file
                                if os.path.exists(local_pdf_path):
                                    try:
                                        os.remove(local_pdf_path)
                                    except OSError as e:
                                        print(
                                            f"Warning: Could not remove temporary file '{local_pdf_path}': {e}"
                                        )
                            except Exception as e:
                                escaped_error = html.escape(str(e), quote=False)
                                print(
                                    f"Could not save page '{page_title}' as PDF: {escaped_error}"
                                )
                                # Clean up temporary PDF file if it exists
                                if os.path.exists(local_pdf_path):
                                    try:
                                        os.remove(local_pdf_path)
                                    except OSError as e:
                                        print(
                                            f"Warning: Could not remove temporary file '{local_pdf_path}': {e}"
                                        )

                        # Also scan the page for files
                        soup = BeautifulSoup(html_body, "html.parser")
                        for link in soup.find_all("a", href=True):
                            if not isinstance(link, Tag):
                                continue
                            href = link.get("href", "")
                            if not isinstance(href, str):
                                continue
                            match = re.search(r"/files/(\d+)", href)
                            if match:
                                file_id_from_page = match.group(1)
                                file_api_url = (
                                    f"{canvas_api_url}/api/v1/files/{file_id_from_page}"
                                )
                                file_info_resp = session.get(
                                    file_api_url,
                                    headers=canvas_headers,
                                    timeout=request_timeout,
                                )
                                if file_info_resp.ok:
                                    new_items_synced += process_canvas_file(
                                        file_info_resp.json(),
                                        page_storage_path,
                                        processed_canvas_file_ids,
                                        canvas_headers,
                                        storage_type,
                                        drive_service,
                                        local_root_dir,
                                        session=session,
                                        timeout=request_timeout,
                                        drive_chunk_size_mb=drive_chunk_size_mb,
                                        summary=summary,
                                        course_name=course_name,
                                        dest_label=f"{course_name}/{page_folder_name}",
                                    )

                except requests.exceptions.RequestException as e:
                    print(f"Could not retrieve details for a module item: {e}")
                except Exception as e:
                    print(f"An unexpected error occurred processing module item: {e}")

        # Merge all course pages into a single PDF
        try:
            new_items_synced += process_course_pages(
                course_id=course_id,
                course_name=course_name,
                course_storage_path_or_id=course_storage_path,
                canvas_api_url=canvas_api_url,
                canvas_headers=canvas_headers,
                storage_type=storage_type,
                drive_service=drive_service,
                session=session,
                timeout=request_timeout,
                per_page=canvas_per_page,
                summary=summary,
            )
        except Exception as e:
            print(f"Error merging course pages for '{course_name}': {e}")

        # --- Course-level reports and exports ---
        if reports_folder_path:
            if export_announcements:
                try:
                    new_items_synced += process_course_announcements(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting announcements for '{course_name}': {e}")

            if export_discussions:
                try:
                    new_items_synced += process_course_discussions(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting discussions for '{course_name}': {e}")

            if export_quizzes:
                try:
                    new_items_synced += process_course_quizzes(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting quizzes for '{course_name}': {e}")

            if export_enrollments:
                try:
                    new_items_synced += process_course_enrollments(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting enrollments for '{course_name}': {e}")

            if export_calendar_events:
                try:
                    new_items_synced += process_course_calendar_events(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting calendar events for '{course_name}': {e}")

            if export_groups:
                try:
                    new_items_synced += process_course_groups(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting groups for '{course_name}': {e}")

            if export_analytics:
                try:
                    new_items_synced += process_course_analytics_activity(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting analytics for '{course_name}': {e}")

            if export_gradebook:
                try:
                    new_items_synced += process_course_gradebook_history(
                        course_id,
                        course_name,
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting gradebook history for '{course_name}': {e}")

            if export_submissions:
                try:
                    new_items_synced += process_course_submissions_summary(
                        course_id,
                        course_name,
                        assignments or [],
                        reports_folder_path,
                        canvas_api_url,
                        canvas_headers,
                        storage_type,
                        drive_service,
                        session=session,
                        timeout=request_timeout,
                        per_page=canvas_per_page,
                        summary=summary,
                    )
                except Exception as e:
                    print(f"Error exporting submissions for '{course_name}': {e}")

        if new_items_synced == 0:
            print(
                "All discoverable files, pages, assignments, and reports for this course are already up to date."
            )
        else:
            print(f"Synced/updated {new_items_synced} item(s) for '{course_name}'.")

    # Global (user-level) inbox conversations archive
    if export_inbox:
        try:
            inbox_changes = process_inbox_conversations(
                root_storage_path,
                canvas_api_url,
                canvas_headers,
                storage_type,
                drive_service,
                session=session,
                timeout=request_timeout,
                per_page=canvas_per_page,
                summary=summary,
            )
            if inbox_changes:
                print(f"Archived {inbox_changes} inbox conversation export(s).")
        except Exception as e:
            print(f"Error exporting inbox conversations: {e}")

    # Print summary before cleanup
    summary.print_summary()

    shutil.rmtree(DOWNLOAD_DIR)
    print("\n--- Sync Complete ---")

    # Prevent automatic exit so users can read the summary, especially when double-clicking an exe
    try:
        input("\nPress Enter to exit...")
    except EOFError:
        # Non-interactive environment; just return
        pass


if __name__ == "__main__":
    main()
