import sys
import os
import platform
import traceback
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import configparser
import logging.handlers
from base64 import b64decode
import json
import jsonschema
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import docker
from minio import Minio
from flask import Flask, render_template, request, jsonify

__version__ = 0.1
__updated__ = '2024-09-24'

TESTRUN = False
DEBUG = False
PROFILE = False

# ------------------------------------------------------------------------------
# Logger for this module.
# ------------------------------------------------------------------------------
logger = None
producer = None

root_url = "http://localhost:8880"

docker_client = None
minio_client = None

containers = {'nlp': None,
              'ip': None,
              'ae': None
             }

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"message": "API is healthy!"})

@app.route('/')
def index():
    return render_template('index.html')

def put_object(bucket, filepath, anon):
    global minio_client
    filename = os.path.basename(filepath)
    print(f"Uploading {filename} to MinIO bucket '{bucket}' in folder '{anon}'")
    result = minio_client.fput_object(
        bucket,
        f"{anon}/{filename}",
        filepath,
        content_type="application/octet-stream"
    )
    print(f"Uploaded {filename} to {bucket}/{anon}/{filename}")


def setup_minio_buckets():
    global minio_client 
    # Create buckets
    found = minio_client.bucket_exists("input")
    if not found:
        minio_client.make_bucket("input")
        print("Created bucket", "input")
    else:
        print("Bucket", "input", "already exists")
    
    found = minio_client.bucket_exists("output")
    if not found:
        minio_client.make_bucket("output")
        print("Created bucket", "output")
    else:
        print("Bucket", "output", "already exists")


def start_anoncontainer(aname):
    """ Start a container
    
    :param aname: Should be one of nlp, autoencoders, classic
    """
    global docker_client
    global containers
    image = "nginx:latest"
    if 'nlp' in aname:
        image = "eunoialabs/anonymizer-nlp:latest"
    elif 'ae' in aname:
        image = "thoth/ae:latest"
    if 'ip' in aname:
        image = "eunoialabs/ipanon:latest"
    try:
        container = docker_client.containers.run(image,
                                                 detach=True, 
                                                 volumes=['/var/run:/var/run'],
                                                 network = "thoth", 
                                                 name=aname)
        # Store this container object in the containers dictionary
        containers[aname] = container
        print("it works")
    except Exception as exc:
        print("it failed")
        print(exc)


def is_container_running(container_name):
    """Verify the status of a container by it's name

    :param container_name: the name of the container
    :return: boolean or None
    """
    global docker_client
    RUNNING = "running"
    try:
        container = docker_client.containers.get(container_name)
    except docker.errors.NotFound as exc:
        print(f"Check container name!\n{exc.explanation}")
    else:
        container_state = container.attrs["State"]
        return container_state["Status"] == RUNNING

def download_file(file_path):
    global logger
    url = f"{root_url}{file_path}"
    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {file_path}: {e}")
        return None
    if response.status_code == 200:
        file_name = os.path.join("data", file_path.split("=")[-1])
        try:
            with open(file_name, 'wb') as file:
                # Write the content of the response to the file
                file.write(response.content)
            return file_path
        except IOError:
            return None
    else:
        return None

container_mapping = {'ip': 'classic', 'nlp': 'nlp', 'ae': 'autoencoders'}

anon = {'csv': 'ae', 'pcap': 'ip', 'log': 'nlp'}

def put_object(bucket, file_path, anon):
    global minio_client
    filename = os.path.basename(file_path)
    result = minio_client.fput_object(bucket, f"{anon}/{filename}", file_path, content_type="application/octet-stream")
    return result

def upload_tominio():
    lsanon = []
    for file in os.listdir('data'):
        print(file)
        if(file.split('.')[-1] in anon.keys()):
            lsanon.append(anon[file.split('.')[-1]])
            put_object('input', f'data/{file}', anon[file.split('.')[-1]])
    return lsanon
        
@app.route('/run_function', methods=['POST'])    
def download_file_from_http():
       
    data = request.get_json()
    fp = data.get('search', '')
    if ((fp.startswith('https')== False) & (fp.startswith('http')== False)):
        fp = 'http://' + fp
    response = requests.get(fp)
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
        # Create a ThreadPoolExecutor with maximum 256 worker threads
        executor = ThreadPoolExecutor(max_workers=256)
         # Use a list to store the download tasks
        tasks = []
        for file_name in file_urls:
            task = executor.submit(download_file, file_name)
            tasks.append(task)
        # Process the completed tasks
        for completed_task in as_completed(tasks):
            result = completed_task.result()
    # required_anons = []
    # Files are downloaded, now upload them to MinIO
    # Go though the files one by one in ./data folder and upload it to minio
    # When ever a new type is found add that to required_anons
    required_anons = upload_tominio()
    # Start the necessary containers
    for anon in required_anons:
        print(anon)
        # if not is_container_running(anon):
        start_anoncontainer(anon)
    
    # Now Wait for completion
    # Check every 5 seconds if containers stop running..
    # once they all stop.
    
    # Download files from Minio - use the output bucket.. 
    # download all files from output bucket.
    # download to anondata folder in the output bucket of minio
    
    # Push files from anon-data to http-server.
        
    
    
    return result

def remove_container_if_exists(container_name):
    client = docker.from_env()
    
    try:
        container = client.containers.get(container_name)  # Get the container by name
        print(f"Stopping and removing container: {container_name}")
        container.stop()  # Stop the container
        container.remove()  # Remove the container
        print(f"Container {container_name} removed successfully.")
    except docker.errors.NotFound:
        print(f"Container {container_name} not found, skipping...")
    except docker.errors.APIError as e:
        print(f"Failed to remove container {container_name}: {str(e)}")

def prune_specific_containers():
    containers_to_prune = ["nlp", "ae", "ip"]
    for container_name in containers_to_prune:
        remove_container_if_exists(container_name)

def download_file_from_minio(file_path):
    global logger
    url = f"{root_url}{file_path}"
    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {file_path}: {e}")
        return None
    if response.status_code == 200:
        file_name = os.path.basename(file_path)
        try:
            with open(file_name, 'wb') as file:
                file.write(response.content)
            file_extension = os.path.splitext(file_name)[1].lower()
            if file_extension == '.pcap':
                target_folder = 'ip'
            elif file_extension == '.log':
                target_folder = 'nlp'
            elif file_extension == '.csv':
                target_folder = 'ae'
            else:
                target_folder = 'others'
            container_name = container_mapping.get(target_folder)
            if container_name and not is_container_running(container_name):
                start_anoncontainer(container_name)
            minio_client.fput_object("output", f"{target_folder}/{file_name}", file_name)
            os.remove(file_name)
            return f"{target_folder}/{file_name}"
        except IOError:
            return None
    else:
        return None


def main(argv=None):
    global docker_client
    global minio_client
    docker_client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
    minio_client = Minio("localhost:9000", 
                            access_key = "admin",
                            secret_key = "adminadmin",
                            secure=False)
    prune_specific_containers()
    setup_minio_buckets()
    app.run(debug=True, host='0.0.0.0', port=8003)

    put_object("input",
                "./collector.log",
                "nlp")

# ------------------------------------------------------------------------------
# MAIN SCRIPT ENTRY POINT.
# ------------------------------------------------------------------------------

if __name__ == '__main__':      # pragma: no cover
    # --------------------------------------------------------------------------
    # Normal operation - call through to the main function.
    # --------------------------------------------------------------------------
    # download_file_from_http('/home/TeAmP0is0N/hackathon/testfile.log')
    sys.exit(main())


# import sys
# import os
# import platform
# import traceback
# import time
# from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
# import configparser
# import logging.handlers
# import requests
# from bs4 import BeautifulSoup
# from concurrent.futures import ThreadPoolExecutor, as_completed
# from tqdm import tqdm
# import docker
# from minio import Minio
# from flask import Flask, render_template, request, jsonify, send_file
# from werkzeug.utils import secure_filename
# import io

# __version__ = 0.1
# __updated__ = '2024-09-24'

# TESTRUN = False
# DEBUG = False
# PROFILE = False

# # ------------------------------------------------------------------------------
# # Logger for this module.
# # ------------------------------------------------------------------------------
# logger = None
# producer = None

# root_url = "http://localhost:8880"

# docker_client = None
# minio_client = None

# containers = {
#     'nlp': None,
#     'classic': None,
#     'autoencoders': None
# }

# app = Flask(__name__)

# UPLOAD_FOLDER = '/tmp/uploads'
# ANON_DATA_FOLDER = 'anondata'
# ALLOWED_EXTENSIONS = {'pcap', 'log', 'csv'}
# app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# app.config['ANON_DATA_FOLDER'] = ANON_DATA_FOLDER

# def allowed_file(filename):
#     return '.' in filename and \
#            filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# @app.route('/health', methods=['GET'])
# def health_check():
#     return jsonify({"message": "API is healthy!"})

# @app.route('/')
# def index():
#     return render_template('index.html')

# def put_object(bucket, filepath, anon):
#     global minio_client
#     filename = os.path.basename(filepath)
#     print(f"Uploading {filename} to MinIO bucket '{bucket}' in folder '{anon}'")
#     result = minio_client.fput_object(
#         bucket,
#         f"{anon}/{filename}",
#         filepath,
#         content_type="application/octet-stream"
#     )
#     print(f"Uploaded {filename} to {bucket}/{anon}/{filename}")

# def setup_minio_buckets():
#     global minio_client 
#     # Create buckets if they don't exist
#     for bucket_name in ["input", "output"]:
#         found = minio_client.bucket_exists(bucket_name)
#         if not found:
#             minio_client.make_bucket(bucket_name)
#             print(f"Created bucket '{bucket_name}'")
#         else:
#             print(f"Bucket '{bucket_name}' already exists")

# def start_anoncontainer(aname):
#     """ Start a Docker container based on the anon name.
    
#     :param aname: Should be one of 'nlp', 'autoencoders', 'classic'
#     """
#     global docker_client
#     global containers
#     image = "ubuntu:latest"  # Default image
#     if 'nlp' in aname:
#         image = "thoth/nlp:latest"
#     elif 'autoencoders' in aname:
#         image = "thoth/ae:latest"
#     elif 'classic' in aname:
#         image = "thoth/classic:latest"
#     try:
#         print(f"Starting container '{aname}' with image '{image}'")
#         container = docker_client.containers.run(
#             image,
#             detach=True, 
#             volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}},
#             name=aname
#         )
#         containers[aname] = container
#         print(f"Container '{aname}' started successfully")
#     except docker.errors.APIError as exc:
#         print(f"Error starting container '{aname}': {exc}")

# def is_container_running(container_name):
#     """Verify if a Docker container is running by its name.
    
#     :param container_name: the name of the container
#     :return: boolean
#     """
#     global docker_client
#     try:
#         container = docker_client.containers.get(container_name)
#         container_state = container.attrs["State"]
#         is_running = container_state["Status"] == "running"
#         print(f"Container '{container_name}' running: {is_running}")
#         return is_running
#     except docker.errors.NotFound as exc:
#         print(f"Container '{container_name}' not found: {exc}")
#         return False

# container_mapping = {'ip': 'classic', 'nlp': 'nlp', 'ae': 'autoencoders'}

# def download_and_upload(file_url):
#     """
#     Downloads a file from the given URL and uploads it to MinIO based on file type.
#     Returns a dict with file URL, file name, and status.
#     """
#     print(f"Downloading: {file_url}")
#     try:
#         response = requests.get(file_url, stream=True)
#         response.raise_for_status()
#     except requests.exceptions.RequestException as e:
#         print(f"Error downloading {file_url}: {e}")
#         return {"file_url": file_url, "file_name": None, "status": "failed", "error": str(e)}
    
#     if response.status_code == 200:
#         file_name = os.path.basename(file_url)
#         try:
#             file_extension = os.path.splitext(file_name)[1].lower()
#             if file_extension == '.pcap':
#                 target_folder = 'ip'
#             elif file_extension == '.log':
#                 target_folder = 'nlp'
#             elif file_extension == '.csv':
#                 target_folder = 'ae'
#             else:
#                 target_folder = 'others'
            
#             # Save the file temporarily
#             temp_file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file_name))
#             os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
#             with open(temp_file_path, 'wb') as file:
#                 for chunk in response.iter_content(chunk_size=8192):
#                     if chunk:  # filter out keep-alive new chunks
#                         file.write(chunk)
#             print(f"Saved file to {temp_file_path}")
            
#             # Upload to MinIO
#             put_object("input", temp_file_path, target_folder)
            
#             # Remove the temporary file
#             os.remove(temp_file_path)
#             print(f"Removed temporary file {temp_file_path}")
            
#             return {"file_url": file_url, "file_name": file_name, "status": "success", "error": None}
#         except IOError as e:
#             print(f"IOError for {file_url}: {e}")
#             return {"file_url": file_url, "file_name": file_name, "status": "failed", "error": str(e)}
#     else:
#         print(f"Failed to download {file_url}, status code {response.status_code}")
#         return {"file_url": file_url, "file_name": None, "status": "failed", "error": f"Status code {response.status_code}"}

# def download_processed_files():
#     """
#     Downloads all files from the 'output' bucket in MinIO to the anondata folder.
#     Returns a list of downloaded file paths.
#     """
#     print("Downloading processed files from MinIO 'output' bucket")
#     os.makedirs(app.config['ANON_DATA_FOLDER'], exist_ok=True)
#     objects = minio_client.list_objects("output", recursive=True)
#     downloaded_files = []
#     for obj in objects:
#         file_path = os.path.join(app.config['ANON_DATA_FOLDER'], obj.object_name.replace('/', '_'))
#         try:
#             print(f"Downloading {obj.object_name} to {file_path}")
#             minio_client.fget_object("output", obj.object_name, file_path)
#             downloaded_files.append(file_path)
#             print(f"Downloaded {obj.object_name} to {file_path}")
#         except Exception as e:
#             print(f"Error downloading {obj.object_name}: {e}")
#     return downloaded_files

# def push_to_http_server(file_paths, destination_url):
#     """
#     Pushes the given files to the specified HTTP server URL.
    
#     :param file_paths: List of file paths to upload
#     :param destination_url: The HTTP server URL to push files to
#     :return: List of upload results
#     """
#     print(f"Pushing files to HTTP server at {destination_url}")
#     results = []
#     for file_path in file_paths:
#         file_name = os.path.basename(file_path)
#         try:
#             with open(file_path, 'rb') as file:
#                 files = {'file': (file_name, file)}
#                 response = requests.post(destination_url, files=files)
#                 response.raise_for_status()
#                 results.append({"file": file_name, "status": "success"})
#                 print(f"Successfully pushed {file_name} to {destination_url}")
#         except requests.exceptions.RequestException as e:
#             results.append({"file": file_name, "status": "failed", "error": str(e)})
#             print(f"Error pushing {file_name} to {destination_url}: {e}")
#     return results

# @app.route('/run_function', methods=['POST'])    
# def run_function():
#     """
#     Endpoint to handle downloading files from a provided URL and uploading them to MinIO.
#     Expects a JSON payload with a 'search' field containing the URL.
#     """
#     data = request.get_json()
#     if not data or 'search' not in data:
#         return jsonify({"error": "No 'search' URL provided"}), 400
#     search_url = data['search']
#     print(f"Fetching from: {search_url}")
    
#     try:
#         response = requests.get(search_url)
#         response.raise_for_status()
#     except requests.exceptions.RequestException as e:
#         print(f"Error fetching {search_url}: {e}")
#         return jsonify({"error": f"Error fetching URL: {str(e)}"}), 400
    
#     if response.status_code == 200:
#         soup = BeautifulSoup(response.content, 'html.parser')
#         links = soup.find_all('a')
#         file_urls = []
#         for link in links:
#             href = link.get('href', '')
#             if 'get' in href:
#                 # Ensure that href is a full URL or relative URL
#                 file_url = href
#                 if not href.startswith('http'):
#                     file_url = requests.compat.urljoin(search_url, href)
#                 file_urls.append(file_url)
        
#         if not file_urls:
#             return jsonify({"message": "No files found to download."}), 200
        
#         print(f"Found {len(file_urls)} files to download.")
        
#         results = []
#         required_anons = set()
        
#         # Create a ThreadPoolExecutor with a reasonable number of worker threads
#         max_workers = min(256, os.cpu_count() + 4)  # Adjust as needed
#         with ThreadPoolExecutor(max_workers=max_workers) as executor:
#             # Submit all download and upload tasks
#             future_to_url = {executor.submit(download_and_upload, url): url for url in file_urls}
            
#             # Optionally, use tqdm for progress bar (server-side logging)
#             for future in as_completed(future_to_url):
#                 url = future_to_url[future]
#                 try:
#                     result = future.result()
#                     results.append(result)
#                     if result['status'] == 'success':
#                         # Determine required anons based on file type
#                         file_extension = os.path.splitext(result['file_name'])[1].lower()
#                         if file_extension == '.pcap':
#                             required_anons.add('ip')
#                         elif file_extension == '.log':
#                             required_anons.add('nlp')
#                         elif file_extension == '.csv':
#                             required_anons.add('ae')
#                         else:
#                             required_anons.add('others')
#                 except Exception as e:
#                     results.append({"file_url": url, "file_name": None, "status": "failed", "error": str(e)})
        
#         # Start necessary containers
#         for anon in required_anons:
#             if anon in container_mapping and not is_container_running(container_mapping[anon]):
#                 start_anoncontainer(container_mapping[anon])
        
#         # Wait for all containers to finish processing
#         print("Waiting for containers to finish processing...")
#         while any(is_container_running(container_mapping[anon]) for anon in required_anons if anon in container_mapping):
#             print("Some containers are still running. Waiting for 5 seconds...")
#             time.sleep(5)
        
#         print("All containers have finished processing.")
        
#         # Download processed files from 'output' bucket
#         processed_files = download_processed_files()
        
#         # Push processed files to another HTTP server
#         # Define the destination URL for pushing files
#         destination_url = "http://localhost:8880/upload_processed"  # Replace with actual URL
#         push_results = push_to_http_server(processed_files, destination_url)
        
#         # Compile the final results
#         final_results = {
#             "download_upload_results": results,
#             "push_results": push_results
#         }
        
#         return jsonify({"message": "Operation completed.", "details": final_results}), 200
#     else:
#         return jsonify({"error": f"Failed to fetch URL, status code {response.status_code}"}), 400

# def download_file_from_minio(filename):
#     """
#     Downloads a specific file from MinIO's 'output' bucket to the anondata folder.
#     """
#     global logger
#     try:
#         data = minio_client.get_object("output", filename)
#         file_path = os.path.join(app.config['ANON_DATA_FOLDER'], os.path.basename(filename))
#         os.makedirs(app.config['ANON_DATA_FOLDER'], exist_ok=True)
#         with open(file_path, 'wb') as file:
#             for chunk in data.stream(32*1024):
#                 file.write(chunk)
#         print(f"Downloaded {filename} to {file_path}")
#         return file_path
#     except Exception as e:
#         print(f"Error downloading {filename}: {e}")
#         return None

# def push_file_to_http_server(file_path, destination_url):
#     """
#     Pushes a single file to the specified HTTP server.
#     """
#     file_name = os.path.basename(file_path)
#     try:
#         with open(file_path, 'rb') as file:
#             files = {'file': (file_name, file)}
#             response = requests.post(destination_url, files=files)
#             response.raise_for_status()
#             print(f"Successfully pushed {file_name} to {destination_url}")
#             return {"file": file_name, "status": "success"}
#     except requests.exceptions.RequestException as e:
#         print(f"Error pushing {file_name} to {destination_url}: {e}")
#         return {"file": file_name, "status": "failed", "error": str(e)}

# def push_to_http_server(file_paths, destination_url):
#     """
#     Pushes multiple files to the specified HTTP server.
#     """
#     results = []
#     for file_path in file_paths:
#         if file_path:
#             result = push_file_to_http_server(file_path, destination_url)
#             results.append(result)
#     return results

# def download_processed_files():
#     """
#     Downloads all files from the 'output' bucket in MinIO to the anondata folder.
#     Returns a list of downloaded file paths.
#     """
#     print("Downloading processed files from MinIO 'output' bucket")
#     os.makedirs(app.config['ANON_DATA_FOLDER'], exist_ok=True)
#     objects = minio_client.list_objects("output", recursive=True)
#     downloaded_files = []
#     for obj in objects:
#         file_path = os.path.join(app.config['ANON_DATA_FOLDER'], obj.object_name.replace('/', '_'))
#         try:
#             print(f"Downloading {obj.object_name} to {file_path}")
#             minio_client.fget_object("output", obj.object_name, file_path)
#             downloaded_files.append(file_path)
#             print(f"Downloaded {obj.object_name} to {file_path}")
#         except Exception as e:
#             print(f"Error downloading {obj.object_name}: {e}")
#     return downloaded_files

# def main(argv=None):
#     '''
#     Main function for the Engine start-up.

#     Called with command-line arguments:
#         *    --config *<file>*
#         *    --verbose

#     Where:

#         *<file>* specifies the path to the configuration file.
#         *verbose* generates more information in the log files.

#     The process listens for REST API invocations and checks them. Errors are
#     displayed to stdout and logged.
#     '''

#     if argv is None:
#         argv = sys.argv
#     else:
#         sys.argv.extend(argv)

#     program_name = os.path.basename(sys.argv[0])
#     program_version = 'v{0}'.format(__version__)
#     program_build_date = str(__updated__)
#     program_version_message = '%%(prog)s {0} ({1})'.format(program_version,
#                                                            program_build_date)

#     try:
#         # ----------------------------------------------------------------------
#         # Setup argument parser so we can parse the command-line.
#         # ----------------------------------------------------------------------
#         parser = ArgumentParser(description="Anonymizer by Thoth",
#                                 formatter_class=ArgumentDefaultsHelpFormatter)
#         parser.add_argument('-v', '--verbose',
#                             dest='verbose',
#                             action='count',
#                             help='set verbosity level')
#         parser.add_argument('-V', '--version',
#                             action='version',
#                             version=program_version_message,
#                             help='Display version information')
#         parser.add_argument('-c', '--config',
#                             dest='config',
#                             default='/etc/opt/att/collector.conf',
#                             help='Use this config file.',
#                             metavar='<file>')
#         parser.add_argument('-s', '--section',
#                             dest='section',
#                             default='defaults',
#                             metavar='<section>',
#                             help='section to use in the config file')
        
#         args = parser.parse_args()
#         verbose = args.verbose
#         config_file = args.config
#         config_section = args.section

#         # ----------------------------------------------------------------------
#         # Now read the config file, using command-line supplied values as
#         # overrides.
#         # ----------------------------------------------------------------------
#         overrides = {}
#         config = configparser.ConfigParser()
#         config['defaults'] = {
#             'log_file': 'engine.log',
#             'vel_port': '12233',
#         }
#         config.read(config_file)

#         log_file = config.get(config_section, 'log_file', fallback='engine.log')

#         # ----------------------------------------------------------------------
#         # Finally we have enough info to start a proper flow trace.
#         # ----------------------------------------------------------------------
#         global logger
#         logger = logging.getLogger('monitor')
#         if ((verbose is not None) and (verbose > 0)):
#             logger.setLevel(logging.DEBUG)
#             logger.info('Verbose mode on')
#         else:
#             logger.setLevel(logging.INFO)
#         handler = logging.handlers.RotatingFileHandler(
#             log_file,
#             maxBytes=1000000,
#             backupCount=10
#         )
#         if (platform.system() == 'Windows'):
#             date_format = '%Y-%m-%d %H:%M:%S'
#         else:
#             date_format = '%Y-%m-%d %H:%M:%S.%f %z'
#         formatter = logging.Formatter(
#             '%(asctime)s %(name)s - %(levelname)s - %(message)s',
#             date_format
#         )
#         handler.setFormatter(formatter)
#         logger.addHandler(handler)
#         logger.info('Started')
        
#         #----------------------------------------------------------------------
#         # manage Clients
#         #----------------------------------------------------------------------
#         global docker_client
#         global minio_client
#         docker_client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
#         minio_client = Minio(
#             "localhost:9000", 
#             access_key = "admin",
#             secret_key = "adminadmin",
#             secure=False
#         )
#         setup_minio_buckets()
#         print("MinIO clients and buckets set up successfully.")
#         logger.info("MinIO clients and buckets set up successfully.")
#         #----------------------------------------------------------------------
#         # Start the httpd server here
#         #----------------------------------------------------------------------
    
#     except KeyboardInterrupt:       # pragma: no cover
#         # ----------------------------------------------------------------------
#         # handle keyboard interrupt
#         # ----------------------------------------------------------------------
#         logger.info('Exiting on keyboard interrupt!')
#         return 0

#     except Exception as e:
#         # ----------------------------------------------------------------------
#         # Handle unexpected exceptions.
#         # ----------------------------------------------------------------------
#         if DEBUG or TESTRUN:
#             raise(e)
#         indent = len(program_name) * ' '
#         sys.stderr.write(program_name + ': ' + repr(e) + '\n')
#         sys.stderr.write(indent + '  for help use --help\n')
#         sys.stderr.write(traceback.format_exc())
#         logger.critical('Exiting because of exception: {0}'.format(e))
#         logger.critical(traceback.format_exc())
#         return 2

# # ------------------------------------------------------------------------------
# # MAIN SCRIPT ENTRY POINT.
# # ------------------------------------------------------------------------------

# if __name__ == '__main__':      # pragma: no cover
#     try:
#         main()
#     except Exception as e:
#         if logger:
#             logger.critical('Failed to start the application: {0}'.format(e))
#         sys.exit(1)
#     # Start the Flask app
#     app.run(debug=True, host='0.0.0.0', port=8003)
