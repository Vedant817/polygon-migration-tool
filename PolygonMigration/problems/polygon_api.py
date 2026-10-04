import hashlib
import time
import random
import string
import requests
from urllib.parse import urlencode
from django.conf import settings
import json
import os
import sys
import zipfile
import tempfile
import redis
import logging
import shutil
import subprocess
import stat

logger = logging.getLogger(__name__)

class PolygonAPI:
    """
    A service class to interact with the Polygon API.
    Handles authentication and provides methods to fetch problem data, test cases, and manage migration to Azure.
    """
    API_URL = "https://polygon.codeforces.com/api/"

    def __init__(self):
        """
        Initializes the PolygonAPI instance with credentials from Django settings.
        """
        self.api_key = settings.POLYGON_API_KEY
        self.api_secret = settings.POLYGON_API_SECRET

    def _generate_api_sig(self, method_name, params):
        """
        Generates the required apiSig for a Polygon API request.

        Args:
            method_name (str): The API method name.
            params (dict): The parameters for the API call.

        Returns:
            tuple: (api_sig (str), time (int))
        """
        # Add apiKey and time to params for signature generation
        params['apiKey'] = self.api_key
        params['time'] = int(time.time())

        # Sort parameters lexicographically
        sorted_params = sorted(params.items())
        
        # Create the parameter string
        param_str = urlencode(sorted_params)
        
        # Generate a 6-character random string
        rand_prefix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        
        # Construct the string to be hashed
        to_hash = f"{rand_prefix}/{method_name}?{param_str}#{self.api_secret}"
        
        # Calculate SHA-512 hash
        sha512_hash = hashlib.sha512(to_hash.encode('utf-8')).hexdigest()
        
        api_sig = f"{rand_prefix}{sha512_hash}"
        
        # Return the final signature and the time parameter used
        return api_sig, params['time']

    def _make_request(self, method_name, params=None, expect_json=True):
        """
        Makes a POST request to the Polygon API.

        Args:
            method_name (str): The API method to call.
            params (dict, optional): The parameters to send. Defaults to None.
            expect_json (bool, optional): Whether to expect and parse JSON response. Defaults to True.

        Returns:
            dict or str: The parsed JSON result or raw text, depending on expect_json.

        Raises:
            Exception: If the API call fails or returns an error.
        """
        if params is None:
            params = {}
        
        api_sig, request_time = self._generate_api_sig(method_name, params.copy())
        
        post_params = params.copy()
        post_params['apiKey'] = self.api_key
        post_params['apiSig'] = api_sig
        post_params['time'] = request_time
        
        try:
            response = requests.post(f"{self.API_URL}{method_name}", data=post_params)
            response.raise_for_status()

            if not expect_json:
                return response.text

            # Only try to parse JSON if we expect it
            try:
                data = response.json()
                if data.get('status') == 'FAILED':
                    raise Exception(f"Polygon API Error: {data.get('comment')}")
                return data.get('result')
            except json.JSONDecodeError:
                if expect_json:
                    raise
                return response.text

        except requests.exceptions.RequestException as e:
            raise Exception(f"HTTP Request Error: {e}")

    def _make_plain_request(self, method_name, params=None):
        """
        Makes a POST request expecting a plain text response.

        Args:
            method_name (str): The API method to call.
            params (dict, optional): The parameters to send. Defaults to None.

        Returns:
            str: The plain text response from the API.
        """
        return self._make_request(method_name, params, expect_json=False)

    def download_and_extract_package(self, problem_id, package_type='standard'):
        """
        Downloads the problem package from Polygon and extracts it to find problem.html.

        Args:
            problem_id (str): The Polygon problem ID.
            package_type (str, optional): The type of package to download. Defaults to 'standard'.

        Returns:
            str: The content of the extracted problem.html file.

        Raises:
            Exception: If the package cannot be downloaded or extracted.
        """
        logger.info("Downloading package for problem %s", problem_id)
        
        # Create a temporary directory for extraction
        temp_dir = tempfile.mkdtemp()
        
        try:
            # First, get the problem info to get the package URL
            problem_info = self.get_problem_info(problem_id)
            if not problem_info:
                raise Exception("Could not get problem info")
            
            # Get the latest package info
            packages = self._make_request('problem.packages', {'problemId': problem_id})
            if not packages:
                raise Exception("No packages available for this problem")
            
            # Find the latest package
            latest_package = None
            for package in packages:
                if package.get('type') == package_type:
                    if not latest_package or package.get('revision', 0) > latest_package.get('revision', 0):
                        latest_package = package
            
            if not latest_package:
                raise Exception(f"No {package_type} package found for this problem")
            
            package_id = latest_package['id']
            
            # Download the package using direct HTTP request to get binary data
            api_sig, request_time = self._generate_api_sig('problem.package', {
                'problemId': problem_id,
                'packageId': package_id,
                'type': package_type
            })
            
            post_params = {
                'problemId': problem_id,
                'packageId': package_id,
                'type': package_type,
                'apiKey': self.api_key,
                'apiSig': api_sig,
                'time': request_time
            }
            
            response = requests.post(f"{self.API_URL}problem.package", data=post_params)
            response.raise_for_status()
            
            # Check if response is actually a zip file
            if not response.content.startswith(b'PK'):
                # If not a zip file, it might be an error message
                try:
                    error_data = response.json()
                    if error_data.get('status') == 'FAILED':
                        raise Exception(f"Polygon API Error: {error_data.get('comment')}")
                except:
                    pass
                raise Exception("Downloaded data is not a valid ZIP file")
            
            # Save the binary package data to a zip file
            zip_path = os.path.join(temp_dir, f'problem_{problem_id}.zip')
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            
            # Extract the zip file
            extract_dir = os.path.join(temp_dir, 'extracted')
            os.makedirs(extract_dir, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            # Find problem.html in the extracted files
            problem_html_path = None
            for root, dirs, files in os.walk(extract_dir):
                for file in files:
                    if file == 'problem.html':
                        problem_html_path = os.path.join(root, file)
                        break
                if problem_html_path:
                    break
            
            if not problem_html_path:
                raise Exception("problem.html not found in the extracted package")
            
            # Read the problem.html content before cleanup
            with open(problem_html_path, 'r', encoding='utf-8') as f:
                problem_html_content = f.read()
            
            logger.info("Successfully read problem.html content, length: %d characters", len(problem_html_content))
            return problem_html_content
            
        except Exception as e:
            raise Exception(f"Error downloading/extracting package: {e}")
        finally:
            # Clean up the temporary directory
            try:
                shutil.rmtree(temp_dir)
                logger.debug("Cleaned up temporary directory: %s", temp_dir)
            except Exception as cleanup_error:
                logger.warning("Failed to clean up temporary directory %s: %s", temp_dir, cleanup_error)

    def get_problem_info(self, problem_id):
        """
        Fetches problem information from Polygon.

        Args:
            problem_id (str): The Polygon problem ID.

        Returns:
            dict: The problem information as returned by the API.
        """
        logger.info("Fetching problem info for %s", problem_id)
        info = self._make_request('problem.info', {'problemId': problem_id})
        logger.info("Polygon problem.info response: %s", info)
        return info

    def get_statements(self, problem_id):
        """
        Fetches problem statements from Polygon.

        Args:
            problem_id (str): The Polygon problem ID.

        Returns:
            dict: The problem statements as returned by the API.
        """
        logger.info("Fetching statements for %s", problem_id)
        return self._make_request('problem.statements', {'problemId': problem_id})

    def get_test_script(self, problem_id, testset='tests'):
        """
        Fetches the test script for a given problem and testset from Polygon.

        Args:
            problem_id (str): The Polygon problem ID.
            testset (str, optional): The testset name. Defaults to 'tests'.

        Returns:
            str: The test script or an error message if not found.
        """
        logger.info("Fetching test script for %s, testset", problem_id)
        try:
            return self._make_request('problem.script', {
                'problemId': problem_id,
                'testset': testset
            })
        except Exception:
            return "Test script not found or could not be fetched."

    def get_test_cases(self, problem_id, testset='tests'):
        """
        Fetches all test cases for a given problem from Polygon.

        Args:
            problem_id (str): The Polygon problem ID.
            testset (str, optional): The testset name. Defaults to 'tests'.

        Returns:
            list: A list of dicts with 'input' and 'output' fields for each test case.
        """
        logger.info("Fetching test cases for %s, testset", problem_id)
        try:
            tests = self._make_request('problem.tests', {
                'problemId': problem_id,
                'testset': testset
            })
            # Each test contains 'input' and 'output' fields
            return tests
        except Exception:
            return []

    def get_problem_files(self, problem_id):
        """
        Fetches all files associated with a problem from Polygon.

        Args:
            problem_id (str): The Polygon problem ID.

        Returns:
            dict: The files information as returned by the API.
        """
        logger.info("Fetching problem files for %s", problem_id)
        try:
            return self._make_request('problem.files', {'problemId': problem_id})
        except Exception:
            return {}

    def get_file_content(self, problem_id, file_type, file_name):
        """
        Fetches the content of a specific file for a problem from Polygon.

        Args:
            problem_id (str): The Polygon problem ID.
            file_type (str): The type of file (e.g., 'resource', 'solution').
            file_name (str): The name of the file.

        Returns:
            str: The file content or an error message if not found.
        """
        logger.info("Fetching content of %s file %s for problem %s", file_type, file_name, problem_id)
        try:
            return self._make_request('problem.viewFile', {
                'problemId': problem_id,
                'type': file_type,
                'name': file_name
            })
        except Exception:
            return f"File {file_name} not found or could not be fetched."

    def get_all_test_cases(self, problem_id, testset='tests'):
        """
        Fetches all test cases (manual and generated) for a given problem from Polygon.
        For all tests, fetches input/output using problem.testInput and problem.testAnswer.

        Args:
            problem_id (str): The Polygon problem ID.
            testset (str, optional): The testset name. Defaults to 'tests'.

        Returns:
            list: A list of dicts with 'input', 'output', 'index', 'manual', and other fields for each test case.
        """
        logger.info("Fetching all test cases for %s, testset", problem_id)
        tests = self._make_request('problem.tests', {'problemId': problem_id, 'testset': testset})
        all_cases = []
        
        logger.info("Found %d test cases to process", len(tests))
        
        for test in tests:
            test_index = test['index']
            logger.info("Processing test case %d/%d (index: %s)", len(all_cases) + 1, len(tests), test_index)
            
            test_case = {
                'index': test_index,
                'manual': test.get('manual', False),
                'is_sample': test.get('useInStatements', False),
                'description': test.get('description', '')
            }
            
            # For all tests, fetch input and output using the API
            try:
                logger.debug("Fetching input for test case %s", test_index)
                test_case['input'] = self._make_plain_request('problem.testInput', {
                    'problemId': problem_id,
                    'testset': testset,
                    'testIndex': test_index
                }) or ''
                
                logger.debug("Fetching output for test case %s", test_index)
                test_case['output'] = self._make_plain_request('problem.testAnswer', {
                    'problemId': problem_id,
                    'testset': testset,
                    'testIndex': test_index
                }) or ''
                
                logger.info("Successfully fetched test case %s - Input length: %d, Output length: %d", 
                          test_index, len(test_case['input']), len(test_case['output']))
                
            except Exception as e:
                logger.warning(f"Error fetching test case {test_index}: {e}")
                test_case['input'] = ''
                test_case['output'] = ''
            
            all_cases.append(test_case)
        
        logger.info("Completed fetching all %d test cases for problem %s", len(all_cases), problem_id)
        return all_cases

    def get_custom_checker_info(self, problem_id):
        """
        Gets information about the custom checker for a problem.
        
        Args:
            problem_id (str): The Polygon problem ID.
            
        Returns:
            dict: Information about the custom checker including name and type.
        """
        logger.info("Fetching custom checker info for problem %s", problem_id)
        try:
            checker_info = self._make_request('problem.checker', {'problemId': problem_id})
            logger.debug("Raw checker info: %s", checker_info)
            if checker_info and not checker_info.startswith('std::'):
                # This is a custom checker
                result = {
                    'name': checker_info,
                    'type': 'custom'
                }
                logger.info("Custom checker detected: %s", result)
                return result
            logger.info("No custom checker found, using standard checker: %s", checker_info)
            return None
        except Exception as e:
            logger.error(f"Error fetching custom checker info: {e}")
            return None

    def fetch_custom_checker_file(self, problem_id, checker_name):
        """
        Fetches the custom checker file content from Polygon API.
        
        Args:
            problem_id (str): The Polygon problem ID.
            checker_name (str): The name of the checker file.
            
        Returns:
            str: The content of the checker file.
        """
        logger.info("Fetching custom checker file %s via API for problem %s", checker_name, problem_id)
        
        try:
            # Try to fetch as source file first (since it's in sourceFiles)
            content = self._make_plain_request('problem.viewFile', {
                'problemId': problem_id,
                'type': 'source',
                'name': checker_name
            })
            logger.info("Successfully fetched custom checker file via API, content length: %d", len(content) if content else 0)
            return content
        except Exception as e:
            logger.error(f"Error fetching custom checker file as source: {e}")
            
            # Try as resource file
            try:
                logger.info("Trying to fetch checker as resource file")
                content = self._make_plain_request('problem.viewFile', {
                    'problemId': problem_id,
                    'type': 'resource',
                    'name': checker_name
                })
                logger.info("Successfully fetched custom checker file as resource, content length: %d", len(content) if content else 0)
                return content
            except Exception as e2:
                logger.error(f"Error fetching custom checker file as resource: {e2}")
                
                # Try with .cpp extension if not already present
                if not checker_name.endswith('.cpp'):
                    try:
                        logger.info("Trying with .cpp extension")
                        content = self._make_plain_request('problem.viewFile', {
                            'problemId': problem_id,
                            'type': 'source',
                            'name': checker_name + '.cpp'
                        })
                        logger.info("Successfully fetched custom checker file with .cpp extension, content length: %d", len(content) if content else 0)
                        return content
                    except Exception as e3:
                        logger.error(f"Error fetching custom checker file with .cpp extension: {e3}")
                
                return None

    def compile_custom_checker(self, source_code, temp_dir):
        """
        Compiles the custom checker source code using g++.
        
        Args:
            source_code (str): The C++ source code.
            temp_dir (str): Temporary directory to work in (deprecated, kept for backward compatibility).
            
        Returns:
            str: Path to the compiled binary, or None if compilation failed.
        """
        # Use CUSTOM_CHECKER_DIR from environment if available
        checker_dir = settings.CUSTOM_CHECKER_DIR
        if checker_dir:
            if not os.path.exists(checker_dir):
                os.makedirs(checker_dir, exist_ok=True)
            temp_dir = checker_dir
        logger.info("Compiling custom checker in %s", temp_dir)
        
        # Check if g++ is available
        gpp_path = shutil.which('g++')
        if not gpp_path:
            logger.error("g++ compiler not found in system PATH. Please install MinGW or another C++ compiler.")
            return None
        
        logger.info("Found g++ at: %s", gpp_path)
        
        # Write source code to file
        source_file = os.path.join(temp_dir, 'custom_checker.cpp')
        with open(source_file, 'w', encoding='utf-8') as f:
            f.write(source_code)
        
        logger.info("Source code written to: %s", source_file)
        
        # Determine binary filename based on operating system
        if sys.platform.startswith('win'):
            binary_filename = 'custom_checker.exe'
        else:
            binary_filename = 'custom_checker'
        
        # Compile using g++
        binary_file = os.path.join(temp_dir, binary_filename)
        try:
            logger.info("Running compilation command: g++ -std=gnu++17 -O2 -o %s %s", binary_file, source_file)
            result = subprocess.run([
                gpp_path, '-std=gnu++17', '-O2', '-o', binary_file, source_file
            ], capture_output=True, text=True, cwd=temp_dir, timeout=30)
            
            logger.info("Compilation completed with return code: %d", result.returncode)
            if result.stdout:
                logger.debug("Compilation stdout: %s", result.stdout)
            if result.stderr:
                logger.debug("Compilation stderr: %s", result.stderr)
            
            if result.returncode == 0:
                # Check if the binary file was actually created
                if os.path.exists(binary_file):
                    # Make the binary executable
                    os.chmod(binary_file, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
                    logger.info("Custom checker compiled successfully")
                    return binary_file
                else:
                    logger.error("Compilation succeeded but binary file was not created")
                    return None
            else:
                logger.error(f"Compilation failed with return code {result.returncode}")
                logger.error(f"stdout: {result.stdout}")
                logger.error(f"stderr: {result.stderr}")
                return None
        except subprocess.TimeoutExpired:
            logger.error("Compilation timed out")
            return None
        except FileNotFoundError as e:
            logger.error(f"g++ compiler not found: {e}")
            return None
        except Exception as e:
            logger.error(f"Error during compilation: {e}")
            return None

    def upload_custom_checker_to_azure(self, problem_id, azure_account_url, azure_tenant_id, azure_client_id, azure_username, azure_password, container_name, db_problem_id=None):
        """
        Fetches, compiles, and uploads custom checker to Azure Blob Storage.
        
        Args:
            problem_id (str): The Polygon problem ID.
            azure_account_url (str): Azure Storage Account URL.
            azure_tenant_id (str): Azure Tenant ID.
            azure_client_id (str): Azure Client ID.
            azure_username (str): Azure username.
            azure_password (str): Azure password.
            container_name (str): Azure Blob container name.
            db_problem_id (str, optional): The database problem ID for naming. Defaults to None.
        """
        sys.path.append('.')
        from problems.AzureTestcase import AzureBlobManager
        
        logger.info("Processing custom checker for problem %s", problem_id)
        
        # Get custom checker info
        checker_info = self.get_custom_checker_info(problem_id)
        if not checker_info:
            logger.info("No custom checker found for problem %s", problem_id)
            return
        
        logger.info("Custom checker info retrieved: %s", checker_info)
        
        # Fetch custom checker source code
        source_code = self.fetch_custom_checker_file(problem_id, checker_info['name'])
        if not source_code:
            logger.error("Failed to fetch custom checker source code")
            return
        
        logger.info("Custom checker source code fetched successfully, length: %d", len(source_code))
        
        # Use CUSTOM_CHECKER_DIR from environment if available
        checker_dir = settings.CUSTOM_CHECKER_DIR
        logger.info("Initializing Azure Blob Manager for custom checker upload")
        blob_manager = AzureBlobManager(
            account_url=azure_account_url,
            tenant_id=azure_tenant_id,
            client_id=azure_client_id,
            username=azure_username,
            password=azure_password
        )
        
        if checker_dir:
            if not os.path.exists(checker_dir):
                os.makedirs(checker_dir, exist_ok=True)
            temp_dir = checker_dir

            # Compile the custom checker
            binary_path = self.compile_custom_checker(source_code, temp_dir)
            if not binary_path:
                logger.warning("Failed to compile custom checker, uploading source code instead")
                # Upload source code as fallback
                try:                    
                    problem_id_for_naming = db_problem_id if db_problem_id is not None else problem_id
                    blob_name = f"test_cases/{problem_id_for_naming}/custom_checker.cpp"
                                        
                    blob_client = blob_manager.blob_service_client.get_blob_client(
                        container=container_name, 
                        blob=blob_name
                    )
                    blob_client.upload_blob(source_code.encode('utf-8'), overwrite=True)
                    logger.info(f"Uploaded custom checker source code to {blob_name}")
                    return
                except Exception as e:
                    logger.error(f"Error uploading custom checker source code: {e}")
                    return
            
            logger.info("Custom checker compiled successfully at: %s", binary_path)
            
            # Read the compiled binary
            try:
                with open(binary_path, 'rb') as f:
                    binary_data = f.read()
                logger.info("Binary file read successfully, size: %d bytes", len(binary_data))
            except Exception as e:
                logger.error(f"Error reading compiled binary: {e}")
                return
            
            problem_id_for_naming = db_problem_id if db_problem_id is not None else problem_id
            
            # Use the same OS detection logic for Azure blob name
            if sys.platform.startswith('win'):
                blob_filename = 'custom_checker.exe'
            else:
                blob_filename = 'custom_checker'
            
            blob_name = f"test_cases/{problem_id_for_naming}/{blob_filename}"
            
            logger.info("Uploading custom checker to blob: %s", blob_name)
            
            try:
                blob_client = blob_manager.blob_service_client.get_blob_client(
                    container=container_name, 
                    blob=blob_name
                )
                blob_client.upload_blob(binary_data, overwrite=True)
                logger.info(f"Successfully uploaded custom checker to {blob_name}")
            except Exception as e:
                logger.error(f"Error uploading custom checker: {e}")
                logger.error(f"Container: {container_name}, Blob: {blob_name}")
                logger.error(f"Binary size: {len(binary_data)} bytes")
        else:
            # Fallback to temporary directory if CUSTOM_CHECKER_DIR is not set
            with tempfile.TemporaryDirectory() as temp_dir:
                logger.info("Created temporary directory for compilation: %s", temp_dir)
                # Compile the custom checker
                binary_path = self.compile_custom_checker(source_code, temp_dir)
                if not binary_path:
                    logger.warning("Failed to compile custom checker, uploading source code instead")
                    # Upload source code as fallback
                    try:                        
                        problem_id_for_naming = db_problem_id if db_problem_id is not None else problem_id
                        blob_name = f"test_cases/{problem_id_for_naming}/custom_checker.cpp"
                                            
                        blob_client = blob_manager.blob_service_client.get_blob_client(
                            container=container_name, 
                            blob=blob_name
                        )
                        blob_client.upload_blob(source_code.encode('utf-8'), overwrite=True)
                        logger.info(f"Uploaded custom checker source code to {blob_name}")
                        return
                    except Exception as e:
                        logger.error(f"Error uploading custom checker source code: {e}")
                        return
                
                logger.info("Custom checker compiled successfully at: %s", binary_path)
                
                # Read the compiled binary
                try:
                    with open(binary_path, 'rb') as f:
                        binary_data = f.read()
                    logger.info("Binary file read successfully, size: %d bytes", len(binary_data))
                except Exception as e:
                    logger.error(f"Error reading compiled binary: {e}")
                    return
                             
                problem_id_for_naming = db_problem_id if db_problem_id is not None else problem_id
                
                # Use the same OS detection logic for Azure blob name
                if sys.platform.startswith('win'):
                    blob_filename = 'custom_checker.exe'
                else:
                    blob_filename = 'custom_checker'
                
                blob_name = f"test_cases/{problem_id_for_naming}/{blob_filename}"
                
                logger.info("Uploading custom checker to blob: %s", blob_name)
                
                try:
                    blob_client = blob_manager.blob_service_client.get_blob_client(
                        container=container_name, 
                        blob=blob_name
                    )
                    blob_client.upload_blob(binary_data, overwrite=True)
                    logger.info(f"Successfully uploaded custom checker to {blob_name}")
                except Exception as e:
                    logger.error(f"Error uploading custom checker: {e}")
                    logger.error(f"Container: {container_name}, Blob: {blob_name}")
                    logger.error(f"Binary size: {len(binary_data)} bytes")

    def migrate_to_azure_blob(self, problem_id, azure_account_url, azure_tenant_id, azure_client_id, azure_username, azure_password, container_name, db_problem_id=None, testset='tests'):
        """
        Fetches all test cases from Redis (or Polygon as fallback) and uploads them to Azure Blob Storage using AzureBlobManager.
        Each test case's input and output are uploaded as separate blobs.
        Only test cases with both input and output are uploaded.
        Also handles custom checker compilation and upload if present.

        Args:
            problem_id (str): The Polygon problem ID.
            azure_account_url (str): Azure Storage Account URL.
            azure_tenant_id (str): Azure Tenant ID.
            azure_client_id (str): Azure Client ID.
            azure_username (str): Azure username.
            azure_password (str): Azure password.
            container_name (str): Azure Blob container name.
            db_problem_id (str, optional): The database problem ID for naming. Defaults to None.
            testset (str, optional): The testset name. Defaults to 'tests'.
        """
        sys.path.append('.')  # Ensure root dir is in path for import
        from problems.AzureTestcase import AzureBlobManager

        logger.info("Starting migrate_to_azure_blob for problem %s", problem_id)

        # Try to get test cases from Redis first
        test_cases = self.get_test_cases_from_redis(problem_id)
        if test_cases is None:
            # Fallback to fetching from Polygon if not in Redis
            logger.warning('Test cases not found in Redis, fetching from Polygon')
            test_cases = self.get_all_test_cases(problem_id, testset)
            # Store them in Redis for future use
            self.store_test_cases_in_redis(problem_id, test_cases, expiry_hours=0.5)
        else:
            logger.info('Retrieved test cases from Redis for Azure migration (saved Polygon API call)')
        
        logger.info("Retrieved %d test cases for Azure migration", len(test_cases))

        blob_manager = AzureBlobManager(
            account_url=azure_account_url,
            tenant_id=azure_tenant_id,
            client_id=azure_client_id,
            username=azure_username,
            password=azure_password
        )

        problem_id_for_naming = db_problem_id if db_problem_id is not None else problem_id
        logger.info("Using problem_id_for_naming: %s", problem_id_for_naming)
        
        # Delete all test cases before updating the new ones
        logger.info("Deleting existing test cases from Azure")
        blob_manager.empty_blob(container_name, problem_id_for_naming)

        uploaded_count = 0
        for idx, test in enumerate(test_cases, start=1):
            input_data = test.get('input', '')
            output_data = test.get('output', '')
            if input_data and output_data:
                # Use database problem ID if provided, otherwise fall back to polygon_id
                blob_manager.upload_test_case(container_name, problem_id_for_naming, idx, input_data, output_data)
                uploaded_count += 1
            else:
                logger.warning(f"Skipping test case #{idx}: missing input or output. input: {repr(input_data[:50] + ('...' if len(input_data) > 50 else ''))}, output: {repr(output_data[:50] + ('...' if len(output_data) > 50 else ''))}")
        
        logger.info("Uploaded %d test cases to Azure", uploaded_count)
        
        # Handle custom checker if present
        logger.info("Starting custom checker processing for problem %s", problem_id)
        self.upload_custom_checker_to_azure(
            problem_id, 
            azure_account_url, 
            azure_tenant_id, 
            azure_client_id, 
            azure_username, 
            azure_password, 
            container_name, 
            db_problem_id
        )
        logger.info("Completed custom checker processing for problem %s", problem_id)
        logger.info("Completed migrate_to_azure_blob for problem %s", problem_id)

    def delete_problem_test_case_cache(self, db_problem_id):
        """
        Deletes all Redis cache keys related to test cases for a given database problem ID.

        Args:
            db_problem_id (str): The database problem ID.
        """
        pattern = f"oj_dev_with_redis_storage_test_cases_{db_problem_id}*"
        logger.info("pattern %s", pattern)
        try:
            REDIS_HOST = settings.REDIS_HOST
            REDIS_PORT = settings.REDIS_PORT
            REDIS_PASSWORD = settings.REDIS_PASSWORD
            REDIS_SSL = settings.REDIS_SSL
            r = redis.StrictRedis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                ssl=REDIS_SSL,
                ssl_cert_reqs=None
            )
            for key in r.scan_iter(pattern):
                logger.info("deleting key %s", key)
                r.delete(key)
                
        except Exception as e:
            logger.error(f"Error deleting cache keys for pattern {pattern}: {e}")

    def store_test_cases_in_redis(self, polygon_id, test_cases, expiry_hours=0.5):
        """
        Stores test cases in Redis with platform-specific keys for this application.
        Each test case is stored as a separate key-value pair.

        Args:
            polygon_id (str): The Polygon problem ID.
            test_cases (list): List of test case dictionaries.
            expiry_hours (float): Expiry time in hours (default 0.5 = 30 minutes).
        """
        try:
            REDIS_HOST = settings.REDIS_HOST
            REDIS_PORT = settings.REDIS_PORT
            REDIS_PASSWORD = settings.REDIS_PASSWORD
            REDIS_SSL = settings.REDIS_SSL
            r = redis.StrictRedis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                ssl=REDIS_SSL,
                ssl_cert_reqs=None
            )
            
            # Platform-specific prefix for this application
            prefix = f"polygon_migration_test_cases_{polygon_id}"
            
            # Store test case count
            count_key = f"{prefix}_count"
            r.setex(count_key, int(expiry_hours * 3600), len(test_cases))
            
            # Store each test case as a separate key
            for idx, test_case in enumerate(test_cases, start=1):
                test_case_key = f"{prefix}_test_{idx}"
                test_case_data = {
                    'index': test_case.get('index', idx),
                    'input': test_case.get('input', ''),
                    'output': test_case.get('output', ''),
                    'description': test_case.get('description', ''),
                    'is_sample': test_case.get('is_sample', False)
                }
                r.setex(test_case_key, int(expiry_hours * 3600), json.dumps(test_case_data))
            
            logger.info(f"Stored {len(test_cases)} test cases in Redis for polygon_id {polygon_id}")
            
        except Exception as e:
            logger.error(f"Error storing test cases in Redis for polygon_id {polygon_id}: {e}")

    def get_test_cases_from_redis(self, polygon_id):
        """
        Retrieves test cases from Redis for the given polygon_id.

        Args:
            polygon_id (str): The Polygon problem ID.

        Returns:
            list: List of test case dictionaries, or None if not found or error.
        """
        try:
            REDIS_HOST = settings.REDIS_HOST
            REDIS_PORT = settings.REDIS_PORT
            REDIS_PASSWORD = settings.REDIS_PASSWORD
            REDIS_SSL = settings.REDIS_SSL
            r = redis.StrictRedis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                ssl=REDIS_SSL,
                ssl_cert_reqs=None
            )
            
            # Platform-specific prefix for this application
            prefix = f"polygon_migration_test_cases_{polygon_id}"
            
            # Get test case count
            count_key = f"{prefix}_count"
            count = r.get(count_key)
            
            if count is None:
                logger.info(f"No test cases found in Redis for polygon_id {polygon_id}")
                return None
            
            count = int(count)
            test_cases = []
            
            # Retrieve each test case
            for idx in range(1, count + 1):
                test_case_key = f"{prefix}_test_{idx}"
                test_case_data = r.get(test_case_key)
                
                if test_case_data:
                    test_case = json.loads(test_case_data)
                    test_cases.append(test_case)
                else:
                    logger.warning(f"Missing test case {idx} in Redis for polygon_id {polygon_id}")
            
            logger.info(f"Retrieved {len(test_cases)} test cases from Redis for polygon_id {polygon_id}")
            return test_cases
            
        except Exception as e:
            logger.error(f"Error retrieving test cases from Redis for polygon_id {polygon_id}: {e}")
            return None

    def clear_test_cases_from_redis(self, polygon_id):
        """
        Clears all test cases from Redis for the given polygon_id.

        Args:
            polygon_id (str): The Polygon problem ID.
        """
        try:
            REDIS_HOST = settings.REDIS_HOST
            REDIS_PORT = settings.REDIS_PORT
            REDIS_PASSWORD = settings.REDIS_PASSWORD
            REDIS_SSL = settings.REDIS_SSL
            r = redis.StrictRedis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                ssl=REDIS_SSL,
                ssl_cert_reqs=None
            )
            
            # Platform-specific prefix for this application
            prefix = f"polygon_migration_test_cases_{polygon_id}"
            pattern = f"{prefix}*"
            
            # Find and delete all keys matching the pattern
            deleted_count = 0
            for key in r.scan_iter(pattern):
                r.delete(key)
                deleted_count += 1
            
            logger.info(f"Cleared {deleted_count} test case keys from Redis for polygon_id {polygon_id}")
            
        except Exception as e:
            logger.error(f"Error clearing test cases from Redis for polygon_id {polygon_id}: {e}")

            