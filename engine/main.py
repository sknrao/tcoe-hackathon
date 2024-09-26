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
              'classic': None,
              'autoencoders': None
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
    print(filename)
    result = minio_client.fput_object(bucket, 
                                      anon + "/" +filename, 
                                      filepath, 
                                      content_type="application/octet-stream")


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
    image = "ubuntu:latest"
    if 'nlp' in aname:
        image = "thoth/nlp:latest"
    elif 'autoencoders' in aname:
        image = "thoth/ae:latest"
    if 'classic' in aname:
        image = "thoth/classic:latest"
    try:
        container = docker_client.containers.run(image,
                                                 detach=True, 
                                                 volumes=['/var/run:/var/run'], 
                                                 name=aname)
        # Store this container object in the containers dictionary
        containers[name] = container
    except Exception as exc:
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

# def download_file(file_path):
#     global logger
#     url = f"{root_url}{file_path}"
#     try:
#         response = requests.get(url)
#         response.raise_for_status()
#     except requests.exceptions.RequestException as e:
#         print(f"Error downloading {file_path}: {e}")
#         return None
#     if response.status_code == 200:
#         file_name = file_path.split("=")[-1]
#         try:
#             with open(file_name, 'wb') as file:
#                 # Write the content of the response to the file
#                 file.write(response.content)
#             return file_path
#         except IOError:
#             return None
#     else:
#         return None

container_mapping = {'ip': 'classic', 'nlp': 'nlp', 'ae': 'autoencoders'}

@app.route('/run_function', methods=['POST'])    
def download_file_from_http():
    data = request.get_json()
    file_path = data.get('search', '')
    # print(file_path)
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
            minio_client.fput_object("input", f"{target_folder}/{file_name}", file_name)
            os.remove(file_name)
            return f"{target_folder}/{file_name}"
        except IOError:
            return None
    else:
        return None

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
    '''
    Main function for the Engine start-up.

    Called with command-line arguments:
        *    --config *<file>*
        *    --verbose

    Where:

        *<file>* specifies the path to the configuration file.
        *verbose* generates more information in the log files.

    The process listens for REST API invocations and checks them. Errors are
    displayed to stdout and logged.
    '''

    if argv is None:
        argv = sys.argv
    else:
        sys.argv.extend(argv)

    program_name = os.path.basename(sys.argv[0])
    program_version = 'v{0}'.format(__version__)
    program_build_date = str(__updated__)
    program_version_message = '%%(prog)s {0} ({1})'.format(program_version,
                                                           program_build_date)

    try:
        # ----------------------------------------------------------------------
        # Setup argument parser so we can parse the command-line.
        # ----------------------------------------------------------------------
        parser = ArgumentParser(description="Anonymizer by Thoth",
                                formatter_class=ArgumentDefaultsHelpFormatter)
        parser.add_argument('-v', '--verbose',
                            dest='verbose',
                            action='count',
                            help='set verbosity level')
        parser.add_argument('-V', '--version',
                            action='version',
                            version=program_version_message,
                            help='Display version information')
        parser.add_argument('-c', '--config',
                            dest='config',
                            default='/etc/opt/att/collector.conf',
                            help='Use this config file.',
                            metavar='<file>')
        parser.add_argument('-s', '--section',
                            dest='section',
                            default='default',
                            metavar='<section>',
                            help='section to use in the config file')
        
        args = parser.parse_args()
        verbose = args.verbose
        config_file = args.config
        config_section = args.section

        # ----------------------------------------------------------------------
        # Now read the config file, using command-line supplied values as
        # overrides.
        # ----------------------------------------------------------------------
        overrides = {}
        config = configparser.ConfigParser()
        config['defaults'] = {'log_file': 'engine.log',
                              'vel_port': '12233',
                              }
        config.read(config_file)

        log_file = config.get(config_section, 'log_file', vars=overrides)

        # ----------------------------------------------------------------------
        # Finally we have enough info to start a proper flow trace.
        # ----------------------------------------------------------------------
        global logger
        logger = logging.getLogger('monitor')
        if ((verbose is not None) and (verbose > 0)):
            logger.info('Verbose mode on')
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        handler = logging.handlers.RotatingFileHandler(log_file,
                                                       maxBytes=1000000,
                                                       backupCount=10)
        if (platform.system() == 'Windows'):
            date_format = '%Y-%m-%d %H:%M:%S'
        else:
            date_format = '%Y-%m-%d %H:%M:%S.%f %z'
        formatter = logging.Formatter('%(asctime)s %(name)s - '
                                      '%(levelname)s - %(message)s',
                                      date_format)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.info('Started')
        
        #----------------------------------------------------------------------
        # manage Clients
        #----------------------------------------------------------------------
        global docker_client
        global minio_client
        docker_client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
        minio_client = Minio("localhost:9000", 
                             access_key = "admin",
                             secret_key = "adminadmin",
                             secure=False)
        setup_minio_buckets()
        put_object("input",
                   "./collector.log",
                   "nlp")
        #----------------------------------------------------------------------
        # Start the httpd server here
        #----------------------------------------------------------------------
    
    except KeyboardInterrupt:       # pragma: no cover
        # ----------------------------------------------------------------------
        # handle keyboard interrupt
        # ----------------------------------------------------------------------
        logger.info('Exiting on keyboard interrupt!')
        return 0

    except Exception as e:
        # ----------------------------------------------------------------------
        # Handle unexpected exceptions.
        # ----------------------------------------------------------------------
        if DEBUG or TESTRUN:
            raise(e)
        indent = len(program_name) * ' '
        sys.stderr.write(program_name + ': ' + repr(e) + '\n')
        sys.stderr.write(indent + '  for help use --help\n')
        sys.stderr.write(traceback.format_exc())
        logger.critical('Exiting because of exception: {0}'.format(e))
        logger.critical(traceback.format_exc())
        return 2


# ------------------------------------------------------------------------------
# MAIN SCRIPT ENTRY POINT.
# ------------------------------------------------------------------------------

if __name__ == '__main__':      # pragma: no cover
    app.run(debug=True, host='0.0.0.0', port=8003)
    # --------------------------------------------------------------------------
    # Normal operation - call through to the main function.
    # --------------------------------------------------------------------------
    # download_file_from_http('/home/TeAmP0is0N/hackathon/testfile.log')
    sys.exit(main())


# import sys
# import os
# import platform
# import traceback
# from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
# import configparser
# import logging.handlers
# import docker
# from minio import Minio

# __version__ = 0.1
# __updated__ = '2024-09-24'

# TESTRUN = False
# DEBUG = False
# PROFILE = False

# logger = None
# producer = None

# root_url = "http://localhost:8880"

# docker_client = None
# minio_client = None

# containers = {'nlp': None,
#               'classic': None,
#               'autoencoders': None
#              }

# container_mapping = {'ip': 'classic', 'nlp': 'nlp', 'ae': 'autoencoders'}

# def put_object(bucket, filepath, anon):
#     global minio_client
#     filename = os.path.basename(filepath)
#     print(filename)
#     result = minio_client.fput_object(bucket, 
#                                       anon + "/" + filename, 
#                                       filepath, 
#                                       content_type="application/octet-stream")

# def setup_minio_buckets():
#     global minio_client
#     # Create buckets
#     for bucket_name in ["input", "output"]:
#         found = minio_client.bucket_exists(bucket_name)
#         if not found:
#             minio_client.make_bucket(bucket_name)
#             print("Created bucket", bucket_name)
#         else:
#             print("Bucket", bucket_name, "already exists")

# def start_anoncontainer(aname):
#     global docker_client
#     global containers
#     image = "ubuntu:latest"
#     if 'nlp' in aname:
#         image = "thoth/nlp:latest"
#     elif 'autoencoders' in aname:
#         image = "thoth/ae:latest"
#     elif 'classic' in aname:
#         image = "thoth/classic:latest"
#     try:
#         container = docker_client.containers.run(image,
#                                                  detach=True, 
#                                                  volumes=['/var/run/docker.sock:/var/run/docker.sock'], 
#                                                  name=aname)
#         containers[aname] = container
#     except Exception as exc:
#         print(exc)

# def is_container_running(container_name):
#     global docker_client
#     RUNNING = "running"
#     try:
#         container = docker_client.containers.get(container_name)
#         container_state = container.attrs["State"]
#         return container_state["Status"] == RUNNING
#     except docker.errors.NotFound:
#         return False

# def download_file_from_http(file_path):
#     global logger
#     try:
#         file_name = os.path.basename(file_path)
#         file_extension = os.path.splitext(file_name)[1].lower()
#         if file_extension == '.pcap':
#             target_folder = 'ip'
#         elif file_extension == '.log':
#             target_folder = 'nlp'
#         elif file_extension == '.csv':
#             target_folder = 'ae'
#         else:
#             target_folder = 'others'
#         container_name = container_mapping.get(target_folder)
#         if container_name and not is_container_running(container_name):
#             start_anoncontainer(container_name)
#         minio_client.fput_object("input", f"{target_folder}/{file_name}", file_path)
#         return f"{target_folder}/{file_name}"
#     except Exception as e:
#         print(f"Error processing {file_path}: {e}")
#         return None

# def main(argv=None):
#     global logger
#     logger = logging.getLogger('monitor')

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
#         # Setup argument parser
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
#                             default='collector.conf',
#                             help='Use this config file.',
#                             metavar='<file>')
#         parser.add_argument('-s', '--section',
#                             dest='section',
#                             default='DEFAULT',
#                             metavar='<section>',
#                             help='section to use in the config file')

#         args = parser.parse_args()
#         verbose = args.verbose
#         config_file = args.config
#         config_section = args.section

#         # Read the config file
#         overrides = {}
#         config = configparser.ConfigParser()
#         config['DEFAULT'] = {'log_file': 'engine.log',
#                              'vel_port': '12233'}
#         if os.path.exists(config_file):
#             config.read(config_file)

#         log_file = config.get(config_section, 'log_file')

#         # Set up logging
#         if ((verbose is not None) and (verbose > 0)):
#             logger.setLevel(logging.DEBUG)
#         else:
#             logger.setLevel(logging.INFO)
#         handler = logging.handlers.RotatingFileHandler(log_file,
#                                                        maxBytes=1000000,
#                                                        backupCount=10)
#         date_format = '%Y-%m-%d %H:%M:%S.%f %z'
#         formatter = logging.Formatter('%(asctime)s %(name)s - '
#                                       '%(levelname)s - %(message)s',
#                                       date_format)
#         handler.setFormatter(formatter)
#         logger.addHandler(handler)
#         logger.info('Started')

#         # Manage Clients
#         global docker_client
#         global minio_client
#         docker_client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
#         minio_client = Minio("localhost:9000",
#                              access_key="qGcrOO6PkUulL4p4yOEg",
#                              secret_key="cbSfXWeMfjFClvNw2FXi3GO8wY1lHDHCKqYcHFWN",
#                              secure=False)
#         setup_minio_buckets()
#         # Start processing
#         download_file_from_http('/home/TeAmP0is0N/hackathon/testfile.log')

#     except KeyboardInterrupt:
#         logger.info('Exiting on keyboard interrupt!')
#         return 0

#     except Exception as e:
#         indent = len(program_name) * ' '
#         sys.stderr.write(program_name + ': ' + repr(e) + '\n')
#         sys.stderr.write(indent + '  for help use --help\n')
#         sys.stderr.write(traceback.format_exc())
#         if logger:
#             logger.critical('Exiting because of exception: {0}'.format(e))
#             logger.critical(traceback.format_exc())
#         return 2

# if __name__ == '__main__':
#     sys.exit(main())

# import sys
# import os
# import platform
# import traceback
# from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
# import configparser
# import logging.handlers
# from minio import Minio

# __version__ = 0.1
# __updated__ = '2024-09-24'

# logger = None

# minio_client = None

# def setup_minio_buckets():
#     global minio_client
#     # Create buckets
#     for bucket_name in ["input", "output"]:
#         found = minio_client.bucket_exists(bucket_name)
#         if not found:
#             minio_client.make_bucket(bucket_name)
#             print("Created bucket", bucket_name)
#         else:
#             print("Bucket", bucket_name, "already exists")

# def download_file_from_http(file_path):
#     global logger
#     try:
#         file_name = os.path.basename(file_path)
#         file_extension = os.path.splitext(file_name)[1].lower()
#         if file_extension == '.pcap':
#             target_folder = 'ip'
#         elif file_extension == '.log':
#             target_folder = 'nlp'
#         elif file_extension == '.csv':
#             target_folder = 'ae'
#         else:
#             target_folder = 'others'
#         minio_client.fput_object("input", f"{target_folder}/{file_name}", file_path)
#         print(f"Uploaded {file_name} to input/{target_folder}/{file_name}")
#         return f"{target_folder}/{file_name}"
#     except Exception as e:
#         print(f"Error processing {file_path}: {e}")
#         return None

# def download_file_from_minio(file_path):
#     global logger
#     try:
#         file_name = os.path.basename(file_path)
#         minio_client.fget_object("output", file_path, file_name)
#         print(f"Downloaded {file_name} from output/{file_path}")
#         return file_name
#     except Exception as e:
#         print(f"Error downloading {file_path}: {e}")
#         return None

# def main(argv=None):
#     global logger
#     logger = logging.getLogger('monitor')

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
#         # Setup argument parser
#         parser = ArgumentParser(description="MinIO Upload/Download Test",
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
#                             default='collector.conf',
#                             help='Use this config file.',
#                             metavar='<file>')
#         parser.add_argument('-s', '--section',
#                             dest='section',
#                             default='DEFAULT',
#                             metavar='<section>',
#                             help='section to use in the config file')

#         args = parser.parse_args()
#         verbose = args.verbose
#         config_file = args.config
#         config_section = args.section

#         # Read the config file
#         overrides = {}
#         config = configparser.ConfigParser()
#         config['DEFAULT'] = {'log_file': 'engine.log',
#                              'vel_port': '12233'}
#         if os.path.exists(config_file):
#             config.read(config_file)

#         log_file = config.get(config_section, 'log_file')

#         # Set up logging
#         if ((verbose is not None) and (verbose > 0)):
#             logger.setLevel(logging.DEBUG)
#         else:
#             logger.setLevel(logging.INFO)
#         handler = logging.handlers.RotatingFileHandler(log_file,
#                                                        maxBytes=1000000,
#                                                        backupCount=10)
#         date_format = '%Y-%m-%d %H:%M:%S.%f %z'
#         formatter = logging.Formatter('%(asctime)s %(name)s - '
#                                       '%(levelname)s - %(message)s',
#                                       date_format)
#         handler.setFormatter(formatter)
#         logger.addHandler(handler)
#         logger.info('Started')

#         # Initialize MinIO client
#         global minio_client
#         minio_client = Minio("localhost:9000",
#                              access_key="qGcrOO6PkUulL4p4yOEg",
#                              secret_key="cbSfXWeMfjFClvNw2FXi3GO8wY1lHDHCKqYcHFWN",
#                              secure=False)
#         setup_minio_buckets()
#         # Upload a file to MinIO
#         download_file_from_http('/home/TeAmP0is0N/hackathon/testfile.log')
#         # Download a file from MinIO
#         download_file_from_minio('nlp/testfile.log')

#     except KeyboardInterrupt:
#         logger.info('Exiting on keyboard interrupt!')
#         return 0

#     except Exception as e:
#         indent = len(program_name) * ' '
#         sys.stderr.write(program_name + ': ' + repr(e) + '\n')
#         sys.stderr.write(indent + '  for help use --help\n')
#         sys.stderr.write(traceback.format_exc())
#         if logger:
#             logger.critical('Exiting because of exception: {0}'.format(e))
#             logger.critical(traceback.format_exc())
#         return 2

# if __name__ == '__main__':
#     sys.exit(main())


# import sys
# import os
# import platform
# import traceback
# from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
# import configparser
# import logging.handlers
# from minio import Minio
# import requests
# from flask import Flask, send_file
# import io

# __version__ = 0.1
# __updated__ = '2024-09-24'

# logger = None

# minio_client = None
# app = Flask(__name__)

# def setup_minio_buckets():
#     global minio_client
#     for bucket_name in ["input", "output"]:
#         found = minio_client.bucket_exists(bucket_name)
#         if not found:
#             minio_client.make_bucket(bucket_name)
#             print("Created bucket", bucket_name)
#         else:
#             print("Bucket", bucket_name, "already exists")

# def download_file_from_http(http_url):
#     global logger
#     try:
#         response = requests.get(http_url)
#         response.raise_for_status()
#         file_data = response.content
#         file_name = os.path.basename(http_url)
#         file_extension = os.path.splitext(file_name)[1].lower()
#         if file_extension == '.pcap':
#             target_folder = 'ip'
#         elif file_extension == '.log':
#             target_folder = 'nlp'
#         elif file_extension == '.csv':
#             target_folder = 'ae'
#         else:
#             target_folder = 'others'
#         minio_client.put_object(
#             bucket_name="input",
#             object_name=f"{target_folder}/{file_name}",
#             data=io.BytesIO(file_data),
#             length=len(file_data),
#             content_type="application/octet-stream"
#         )
#         print(f"Uploaded {file_name} to input/{target_folder}/{file_name}")
#         return f"{target_folder}/{file_name}"
#     except Exception as e:
#         print(f"Error processing {http_url}: {e}")
#         return None

# @app.route('/download/<path:object_name>')
# def download_file_from_minio(object_name):
#     global logger
#     try:
#         data = minio_client.get_object("output", object_name)
#         return send_file(
#             io.BytesIO(data.read()),
#             attachment_filename=os.path.basename(object_name),
#             as_attachment=True
#         )
#     except Exception as e:
#         print(f"Error downloading {object_name}: {e}")
#         return "Error downloading file", 500

# def main(argv=None):
#     global logger
#     logger = logging.getLogger('monitor')

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
#         parser = ArgumentParser(description="MinIO Upload/Download Test",
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
#                             default='collector.conf',
#                             help='Use this config file.',
#                             metavar='<file>')
#         parser.add_argument('-s', '--section',
#                             dest='section',
#                             default='DEFAULT',
#                             metavar='<section>',
#                             help='section to use in the config file')

#         args = parser.parse_args()
#         verbose = args.verbose
#         config_file = args.config
#         config_section = args.section

#         overrides = {}
#         config = configparser.ConfigParser()
#         config['DEFAULT'] = {'log_file': 'engine.log',
#                              'vel_port': '12233'}
#         if os.path.exists(config_file):
#             config.read(config_file)

#         log_file = config.get(config_section, 'log_file')

#         if ((verbose is not None) and (verbose > 0)):
#             logger.setLevel(logging.DEBUG)
#         else:
#             logger.setLevel(logging.INFO)
#         handler = logging.handlers.RotatingFileHandler(log_file,
#                                                        maxBytes=1000000,
#                                                        backupCount=10)
#         date_format = '%Y-%m-%d %H:%M:%S.%f %z'
#         formatter = logging.Formatter('%(asctime)s %(name)s - '
#                                       '%(levelname)s - %(message)s',
#                                       date_format)
#         handler.setFormatter(formatter)
#         logger.addHandler(handler)
#         logger.info('Started')

#         global minio_client
#         minio_client = Minio("localhost:9000",
#                              access_key="qGcrOO6PkUulL4p4yOEg",
#                              secret_key="cbSfXWeMfjFClvNw2FXi3GO8wY1lHDHCKqYcHFWN",
#                              secure=False)
#         setup_minio_buckets()

#         download_file_from_http('http://localhost:8880/testfile.log')

#         app.run(host='0.0.0.0', port=8881)

#     except KeyboardInterrupt:
#         logger.info('Exiting on keyboard interrupt!')
#         return 0

#     except Exception as e:
#         indent = len(program_name) * ' '
#         sys.stderr.write(program_name + ': ' + repr(e) + '\n')
#         sys.stderr.write(indent + '  for help use --help\n')
#         sys.stderr.write(traceback.format_exc())
#         if logger:
#             logger.critical('Exiting because of exception: {0}'.format(e))
#             logger.critical(traceback.format_exc())
#         return 2

# if __name__ == '__main__':
#     sys.exit(main())

