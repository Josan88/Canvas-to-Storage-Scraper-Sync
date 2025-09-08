import os
import requests
import configparser
import shutil
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem,
)
from reportlab.lib.units import inch
from reportlab.lib.colors import blue
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from datetime import datetime
import html

# --- Configuration ---
SCOPES = ["https://www.googleapis.com/auth/drive"]
CONFIG_FILE = "config.ini"
GOOGLE_CREDS_FILE = "credentials.json"
GOOGLE_TOKEN_FILE = "token.json"
DOWNLOAD_DIR = "temp_canvas_downloads"


# --- Helper Functions ---
def sanitize_filename(name):
    """Removes invalid characters from a string to make it a valid filename."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def get_existing_file_metadata_drive(service, folder_id, filename):
    """Gets metadata of an existing file in Google Drive folder."""
    if not folder_id or not filename:
        return None
    try:
        escaped_name = filename.replace("'", "\\'")
        query = f"name='{escaped_name}' and '{folder_id}' in parents"
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
            backColor="#f0f0f0",
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
    for element in soup.children:
        if element.name or (
            hasattr(element, "string") and element.string and element.string.strip()
        ):
            process_element(element)

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
    service, local_path, drive_filename, folder_id, existing_file_id=None
):
    """Uploads a single file to the specified Google Drive folder, or updates if existing_file_id provided."""
    if not os.path.exists(local_path):
        return False
    try:
        if existing_file_id:
            print(f"Updating '{drive_filename}' in Google Drive...")
            media = MediaFileUpload(local_path)
            service.files().update(fileId=existing_file_id, media_body=media).execute()
        else:
            print(f"Uploading '{drive_filename}' to Google Drive...")
            file_metadata = {"name": drive_filename, "parents": [folder_id]}
            # Specify mimetype for HTML files for better browser handling
            mimetype = "text/html" if drive_filename.lower().endswith(".html") else None
            media = MediaFileUpload(local_path, mimetype=mimetype)
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


def get_paginated_canvas_items(url, headers):
    """Handles Canvas API pagination to retrieve all items from an endpoint."""
    items, next_url = [], url
    while next_url:
        try:
            response = requests.get(next_url, headers=headers)
            response.raise_for_status()
            items.extend(response.json())
            next_url = None
            if "Link" in response.headers:
                links = requests.utils.parse_header_links(response.headers["Link"])
                next_url = next(
                    (link["url"] for link in links if link.get("rel") == "next"), None
                )
        except requests.exceptions.RequestException as e:
            print(f"Error fetching data from Canvas: {e}")
            break
    return items


def download_canvas_file(file_url, local_path, headers):
    """Downloads a file from a Canvas URL to a local path."""
    try:
        with requests.get(file_url, headers=headers, stream=True) as r:
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
    existing_files,
    processed_canvas_file_ids,
    canvas_headers,
    storage_type,
    drive_service=None,
    local_root_dir=None,
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
    if download_canvas_file(file_download_url, local_filepath, canvas_headers):
        existing_file_id = existing_metadata.get("id") if existing_metadata else None
        if storage_type == "google_drive":
            success = upload_file_to_drive(
                drive_service,
                local_filepath,
                filename,
                folder_path_or_id,
                existing_file_id,
            )
        else:  # local storage
            success = save_file_locally(local_filepath, filename, folder_path_or_id)

        if success:
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
):
    """Saves an assignment's details and linked files."""
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

    # Check if assignment has changed
    if not has_file_changed(existing_metadata, canvas_updated_at=updated_at):
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
                                    # Process HTML content properly
                                    html_elements = html_to_pdf_elements(
                                        f"<i>{criterion_long_desc}</i>", styles
                                    )
                                    content.extend(html_elements)
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
                                                # Process HTML content properly
                                                html_elements = html_to_pdf_elements(
                                                    f"    <i>{rating_long_desc}</i>",
                                                    styles,
                                                )
                                                content.extend(html_elements)
                                                content.append(Spacer(1, 2))

                                            # Add small description if available and different
                                            elif (
                                                rating_small_desc
                                                and rating_small_desc.strip()
                                                and rating_small_desc != rating_desc
                                            ):
                                                # Process HTML content properly
                                                html_elements = html_to_pdf_elements(
                                                    f"    <i>{rating_small_desc}</i>",
                                                    styles,
                                                )
                                                content.extend(html_elements)
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
            # Clean up temporary PDF file
            if os.path.exists(local_pdf_path):
                os.remove(local_pdf_path)
        except Exception as e:
            escaped_error = html.escape(str(e), quote=False)
            print(
                f"Could not save assignment '{assignment_name}' as PDF: {escaped_error}"
            )
            # Clean up temporary PDF file if it exists
            if os.path.exists(local_pdf_path):
                os.remove(local_pdf_path)

    # Get existing files for linked files processing
    if storage_type == "google_drive":
        existing_files = get_existing_files_in_drive_folder(
            drive_service, assignment_storage_path
        )
    else:
        existing_files = get_existing_files_in_local_folder(assignment_storage_path)

    # Scan the assignment description for linked files
    if description:
        soup = BeautifulSoup(description, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            match = re.search(r"/files/(\d+)", href)
            if match:
                file_id = match.group(1)
                file_api_url = f"{canvas_api_url}/api/v1/files/{file_id}"
                try:
                    file_info_resp = requests.get(file_api_url, headers=canvas_headers)
                    file_info_resp.raise_for_status()
                    if file_info_resp.ok:
                        new_items_count += process_canvas_file(
                            file_info_resp.json(),
                            assignment_storage_path,
                            existing_files,
                            processed_canvas_file_ids,
                            canvas_headers,
                            storage_type,
                            drive_service,
                            local_root_dir,
                        )
                except requests.RequestException as e:
                    print(f"Could not fetch file link from assignment: {e}")

    return new_items_count


def main():
    """Main function to run the sync process."""
    print("--- Starting Canvas to Storage Sync ---")

    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: Config file '{CONFIG_FILE}' not found.")
        return
    config.read(CONFIG_FILE)

    try:
        canvas_api_url = config["CANVAS"]["API_URL"]
        canvas_api_key = config["CANVAS"]["API_KEY"]
        storage_type = config["STORAGE"]["STORAGE_TYPE"].lower()

        local_root_dir = None
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
        root_storage_path = os.path.abspath(local_root_dir)
        if not os.path.exists(root_storage_path):
            os.makedirs(root_storage_path)
        print(f"Syncing to local directory: '{root_storage_path}'")

    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR)

    print("\nFetching courses from Canvas...")
    courses_url = f"{canvas_api_url}/api/v1/courses"
    courses = get_paginated_canvas_items(courses_url, canvas_headers)
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

        if storage_type == "google_drive":
            course_storage_path = get_or_create_folder(
                drive_service, course_name, parent_id=root_storage_path
            )
            if not course_storage_path:
                continue
            existing_files_in_course_folder = get_existing_files_in_drive_folder(
                drive_service, course_storage_path
            )
        else:  # local storage
            course_storage_path = get_or_create_local_folder(
                local_root_dir, course_name
            )
            existing_files_in_course_folder = get_existing_files_in_local_folder(
                course_storage_path
            )

        processed_canvas_file_ids = set()
        new_items_synced = 0

        # --- Process Assignments ---
        print("Searching for assignments...")
        assignments_url = (
            f"{canvas_api_url}/api/v1/courses/{course_id}/assignments?include[]=rubric"
        )
        assignments = get_paginated_canvas_items(assignments_url, canvas_headers)
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
                    )

        # --- Process Modules (Files and Pages) ---
        print("Searching for files and pages in modules...")
        modules_url = f"{canvas_api_url}/api/v1/courses/{course_id}/modules"
        modules = get_paginated_canvas_items(modules_url, canvas_headers)

        for module in modules:
            items_url = f"{canvas_api_url}/api/v1/courses/{course_id}/modules/{module['id']}/items"
            module_items = get_paginated_canvas_items(items_url, canvas_headers)

            for item in module_items:
                try:
                    # Case 1: Item is a direct file link
                    if item.get("type") == "File":
                        file_details_resp = requests.get(
                            item["url"], headers=canvas_headers
                        )
                        file_details_resp.raise_for_status()
                        new_items_synced += process_canvas_file(
                            file_details_resp.json(),
                            course_storage_path,
                            existing_files_in_course_folder,
                            processed_canvas_file_ids,
                            canvas_headers,
                            storage_type,
                            drive_service,
                            local_root_dir,
                        )

                    # Case 2: Item is a Page, which we save as an HTML file
                    elif item.get("type") == "Page":
                        page_resp = requests.get(item["url"], headers=canvas_headers)
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
                            page_existing_files = get_existing_files_in_drive_folder(
                                drive_service, page_storage_path
                            )
                        else:  # local storage
                            page_storage_path = get_or_create_local_folder(
                                course_storage_path, page_folder_name
                            )
                            page_existing_files = get_existing_files_in_local_folder(
                                page_storage_path
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
                                # Clean up temporary PDF file
                                if os.path.exists(local_pdf_path):
                                    os.remove(local_pdf_path)
                            except Exception as e:
                                escaped_error = html.escape(str(e), quote=False)
                                print(
                                    f"Could not save page '{page_title}' as PDF: {escaped_error}"
                                )
                                # Clean up temporary PDF file if it exists
                                if os.path.exists(local_pdf_path):
                                    os.remove(local_pdf_path)

                        # Also scan the page for files
                        soup = BeautifulSoup(html_body, "html.parser")
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            match = re.search(r"/files/(\d+)", href)
                            if match:
                                file_id_from_page = match.group(1)
                                file_api_url = (
                                    f"{canvas_api_url}/api/v1/files/{file_id_from_page}"
                                )
                                file_info_resp = requests.get(
                                    file_api_url, headers=canvas_headers
                                )
                                if file_info_resp.ok:
                                    new_items_synced += process_canvas_file(
                                        file_info_resp.json(),
                                        page_storage_path,
                                        page_existing_files,
                                        processed_canvas_file_ids,
                                        canvas_headers,
                                        storage_type,
                                        drive_service,
                                        local_root_dir,
                                    )

                except requests.exceptions.RequestException as e:
                    print(f"Could not retrieve details for a module item: {e}")
                except Exception as e:
                    print(f"An unexpected error occurred processing module item: {e}")

        if new_items_synced == 0:
            print(
                "All discoverable files, pages, and assignments for this course are already up to date."
            )
        else:
            print(f"Synced/updated {new_items_synced} item(s) for '{course_name}'.")

    shutil.rmtree(DOWNLOAD_DIR)
    print("\n--- Sync Complete ---")


if __name__ == "__main__":
    main()
