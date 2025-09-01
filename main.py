import os
import requests
import configparser
import shutil
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

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


def display_courses_and_get_selection(courses):
    """Displays available courses and gets user selection."""
    print("\nAvailable courses:")
    for i, course in enumerate(courses, 1):
        course_name = course.get("name", "Unnamed")
        course_code = course.get("course_code", "")
        print(f"{i}. {course_name} ({course_code})")

    print("\nOptions:")
    print("- Enter course numbers separated by commas (e.g., 1,3,5)")
    print("- Enter 'all' to select all courses")
    print("- Enter 'quit' to exit")

    while True:
        try:
            user_input = input("\nSelect courses to sync: ").strip().lower()

            if user_input == "quit":
                return []

            if user_input == "all":
                return courses

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
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
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


def upload_file_to_drive(service, local_path, drive_filename, folder_id):
    """Uploads a single file to the specified Google Drive folder."""
    if not os.path.exists(local_path):
        return False
    try:
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
        print(f"An error occurred during file upload: {error}")
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

    if (
        not all([file_id, filename, file_download_url])
        or file_id in processed_canvas_file_ids
    ):
        return 0

    processed_canvas_file_ids.add(file_id)

    if filename in existing_files:
        return 0

    print(f"New file found: '{filename}'")
    local_filepath = os.path.join(DOWNLOAD_DIR, filename)
    if download_canvas_file(file_download_url, local_filepath, canvas_headers):
        if storage_type == "google_drive":
            success = upload_file_to_drive(
                drive_service, local_filepath, filename, folder_path_or_id
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

    # Get user selection
    selected_courses = display_courses_and_get_selection(available_courses)

    if not selected_courses:
        print("No courses selected. Exiting.")
        return

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
            existing_files = get_existing_files_in_drive_folder(
                drive_service, course_storage_path
            )
        else:  # local storage
            course_storage_path = get_or_create_local_folder(
                local_root_dir, course_name
            )
            existing_files = get_existing_files_in_local_folder(course_storage_path)

        processed_canvas_file_ids = set()
        new_items_synced = 0
        processed_canvas_file_ids = set()
        new_items_synced = 0

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
                            existing_files,
                            processed_canvas_file_ids,
                            canvas_headers,
                            storage_type,
                            drive_service if storage_type == "google_drive" else None,
                            local_root_dir if storage_type == "local" else None,
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

                        html_filename = f"{safe_page_title}.html"

                        if html_filename in page_existing_files:
                            continue

                        print(f"New page found: '{page_title}'")
                        local_html_path = os.path.join(DOWNLOAD_DIR, html_filename)
                        # Create a full, well-formed HTML document
                        full_html = f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{page_title}</title></head><body>{html_body}</body></html>'

                        try:
                            # Write the HTML content to a local file
                            with open(local_html_path, "w", encoding="utf-8") as f:
                                f.write(full_html)

                            if storage_type == "google_drive":
                                success = upload_file_to_drive(
                                    drive_service,
                                    local_html_path,
                                    html_filename,
                                    page_storage_path,
                                )
                            else:  # local storage
                                success = save_file_locally(
                                    local_html_path,
                                    html_filename,
                                    page_storage_path,
                                )

                            if success:
                                new_items_synced += 1
                        except Exception as e:
                            print(
                                f"\n[ERROR] Could not save page '{page_title}' as HTML: {e}\n"
                            )

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
                                        (
                                            drive_service
                                            if storage_type == "google_drive"
                                            else None
                                        ),
                                        (
                                            local_root_dir
                                            if storage_type == "local"
                                            else None
                                        ),
                                    )

                except requests.exceptions.RequestException as e:
                    print(f"Could not retrieve details for a module item: {e}")
                except Exception as e:
                    print(f"An unexpected error occurred processing module item: {e}")

        if new_items_synced == 0:
            print(
                "All discoverable files and pages for this course are already in sync."
            )
        else:
            print(f"Synced {new_items_synced} new item(s) for '{course_name}'.")

    shutil.rmtree(DOWNLOAD_DIR)
    print("\n--- Sync Complete ---")


if __name__ == "__main__":
    main()
