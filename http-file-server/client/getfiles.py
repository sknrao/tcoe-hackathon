import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from tqdm import tqdm
from PIL import Image
from io import BytesIO

root_url = "http://localhost:8880"

def download_file(file_path):
    url = f"{root_url}{file_path}"
    print(url)
    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {file_path}: {e}")
        return None
    if response.status_code == 200:
        # Extract the directory and filename from the file path
        #directory, filename = os.path.split(file_path)
        # Create the directory if it doesn't exist
        # os.makedirs(directory, exist_ok=True)
        # Save the file as JPEG
        file_name = file_path.split("=")[-1]
        print(file_name)
        try:
            with open(file_name, 'wb') as file:
                # Write the content of the response to the file
                file.write(response.content)
            return file_path
        except IOError:
            return None
    else:
        print("Hello")
        return None

# Send an HTTP GET request to the URL
response = requests.get(root_url)

# Check if the request was successful
if response.status_code == 200:
    # Parse the HTML content of the response
    soup = BeautifulSoup(response.content, 'html.parser')

    # Find all <a> tags that represent links
    links = soup.find_all('a')

    # Extract the href attribute of each link
    file_urls = []
    for link in links:
        if 'get' in link['href']:
            file_urls.append(link['href'])

    for file_name in file_urls:
        print(file_name)
    # Create a ThreadPoolExecutor with maximum 256 worker threads
    executor = ThreadPoolExecutor(max_workers=256)

    # Use a list to store the download tasks
    tasks = []

    # Use tqdm to create a progress bar
    with tqdm(total=len(file_urls)) as progress_bar:
        error_count = 0
        # Submit the download tasks
        for file_name in file_urls:
            task = executor.submit(download_file, file_name)
            tasks.append(task)

        # Process the completed tasks
        for completed_task in as_completed(tasks):
            result = completed_task.result()
            if result is None:
                error_count += 1
            progress_bar.update(1)

    print(f"All downloads completed, errors: {error_count}")
