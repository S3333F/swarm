import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import bittensor as bt

from swarm.protocol import MapTask, ValidationResult
from swarm.constants import EVAL_TIMEOUT_SEC


class DockerSecureEvaluator:
    """Docker-based secure model evaluation"""
    
    _instance = None
    _base_ready = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        # Only initialize attributes on first instantiation
        if not hasattr(self, 'base_image'):
            self.base_image = "swarm_evaluator_base:latest"
            self.last_fake_model_info = None
        
        if not DockerSecureEvaluator._base_ready:
            self._setup_base_container()
            DockerSecureEvaluator._base_ready = self.base_ready
    
    def _ensure_docker_installed(self):
        """Check if Docker is installed, install automatically if missing"""
        try:
            # Check if Docker command exists
            result = subprocess.run(["docker", "--version"], 
                                  capture_output=True, text=True, check=True)
            bt.logging.info(f"Docker found: {result.stdout.strip()}")
            return True
            
        except (subprocess.CalledProcessError, FileNotFoundError):
            bt.logging.warning("🐳 Docker not found - installing automatically...")
            
            try:
                # Install Docker using official script
                bt.logging.info("Downloading Docker installation script...")
                
                # Download Docker install script
                download_cmd = ["curl", "-fsSL", "https://get.docker.com", "-o", "/tmp/get-docker.sh"]
                subprocess.run(download_cmd, check=True, capture_output=True)
                
                # Make script executable
                subprocess.run(["chmod", "+x", "/tmp/get-docker.sh"], check=True)
                
                # Run Docker installation
                bt.logging.info("Installing Docker (this may take a few minutes)...")
                install_result = subprocess.run(["sudo", "/tmp/get-docker.sh"], 
                                              capture_output=True, text=True, timeout=300)
                
                if install_result.returncode == 0:
                    bt.logging.info("✅ Docker installed successfully!")
                    
                    # Add current user to docker group
                    import os
                    username = os.getenv("USER", "root")
                    subprocess.run(["sudo", "usermod", "-aG", "docker", username], 
                                 capture_output=True)
                    
                    # Start Docker service
                    subprocess.run(["sudo", "systemctl", "start", "docker"], 
                                 capture_output=True)
                    subprocess.run(["sudo", "systemctl", "enable", "docker"], 
                                 capture_output=True)
                    
                    bt.logging.info("🔄 Docker service started")
                    
                    # Cleanup
                    subprocess.run(["rm", "-f", "/tmp/get-docker.sh"], capture_output=True)
                    
                    # Wait a moment for Docker to be ready
                    import time
                    time.sleep(3)
                    
                    # Verify installation
                    verify_result = subprocess.run(["docker", "--version"], 
                                                 capture_output=True, text=True)
                    if verify_result.returncode == 0:
                        bt.logging.info(f"✅ Docker ready: {verify_result.stdout.strip()}")
                        return True
                    else:
                        bt.logging.error("❌ Docker installation verification failed")
                        return False
                        
                else:
                    bt.logging.error(f"❌ Docker installation failed: {install_result.stderr}")
                    return False
                    
            except subprocess.TimeoutExpired:
                bt.logging.error("❌ Docker installation timed out")
                return False
            except Exception as e:
                bt.logging.error(f"❌ Docker installation error: {e}")
                return False
    
    def _setup_base_container(self):
        """Build base Docker image with all dependencies"""
        try:
            # Check if Docker is installed, install if missing
            self._ensure_docker_installed()
            
            # Aggressive cleanup to prevent disk bloat from dangling images/containers
            try:
                # Remove stopped containers and any leftover eval/verify containers
                subprocess.run(["docker", "container", "prune", "-f"], capture_output=True)
                subprocess.run("docker rm -f $(docker ps -aq --filter=name=swarm_eval_)", shell=True, capture_output=True)
                subprocess.run("docker rm -f $(docker ps -aq --filter=name=swarm_verify_)", shell=True, capture_output=True)
            except Exception:
                pass
            try:
                # Remove only dangling images (not all unused images)
                # This prevents accidentally removing the base image
                subprocess.run(["docker", "image", "prune", "-f"], capture_output=True)
                # Remove builder cache to reclaim space
                subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True)
                subprocess.run(["docker", "volume", "prune", "-f"], capture_output=True)
            except Exception:
                pass
            
            dockerfile_path = Path(__file__).parent / "Dockerfile"
            # Build context should be the parent of swarm package
            build_context = Path(__file__).parent.parent.parent
            
            # Build base image (always fresh to get latest swarm scripts)
            cmd = [
                "docker", "build",
                "--no-cache",
                "-t", self.base_image,
                "-f", str(dockerfile_path),
                str(build_context)
            ]
            
            bt.logging.info("Building base Docker image (this may take a few minutes)...")
            bt.logging.debug(f"Docker build command: {' '.join(cmd)}")
            
            # Run with real-time output so user can see progress
            result = subprocess.run(cmd, text=True)
            
            if result.returncode == 0:
                self.base_ready = True
                DockerSecureEvaluator._base_ready = True
                bt.logging.info("✅ Base Docker image ready")
            else:
                bt.logging.error(f"❌ Docker build failed with return code: {result.returncode}")
                self.base_ready = False
                DockerSecureEvaluator._base_ready = False
                
        except Exception as e:
            bt.logging.error(f"Failed to setup Docker environment: {e}")
            self.base_ready = False
            DockerSecureEvaluator._base_ready = False
    

    async def evaluate_model(
        self, 
        task: MapTask, 
        uid: int, 
        model_path: Path
    ) -> ValidationResult:
        """Evaluate model in isolated Docker container with proper lifecycle management"""
        
        if not DockerSecureEvaluator._base_ready:
            bt.logging.warning(f"Docker not ready for UID {uid}")
            return ValidationResult(uid, False, 0.0, 0.0, 0.0)
        
        # Double-check that base image exists before proceeding
        try:
            check_result = subprocess.run(
                ["docker", "images", "-q", self.base_image],
                capture_output=True,
                text=True
            )
            if not check_result.stdout.strip():
                bt.logging.error(f"Base image {self.base_image} not found - rebuilding...")
                self._setup_base_container()
                if not DockerSecureEvaluator._base_ready:
                    bt.logging.error(f"Failed to rebuild base image for UID {uid}")
                    return ValidationResult(uid, False, 0.0, 0.0, 0.0)
        except Exception as e:
            bt.logging.warning(f"Failed to check for base image: {e}")
            return ValidationResult(uid, False, 0.0, 0.0, 0.0)
        
        # Track fake models detected for this evaluation
        self.last_fake_model_info = None
        
        # Validate model has secure metadata before Docker execution
        try:
            import zipfile
            with zipfile.ZipFile(model_path, 'r') as zf:
                if "safe_policy_meta.json" not in zf.namelist():
                    bt.logging.warning(f"Model {uid} missing secure metadata")
                    return ValidationResult(uid, False, 0.0, 0.0, 0.0)
        except Exception as e:
            bt.logging.warning(f"Failed to validate model {uid}: {e}")
            return ValidationResult(uid, False, 0.0, 0.0, 0.0)
        
        container_name = f"swarm_eval_{uid}_{int(time.time() * 1000)}"
        
        # ENHANCED LOGGING: Starting Docker for UID
        bt.logging.info(f"🐳 Starting Docker container for UID {uid} evaluation...")
        
        try:
            # Create temp directory for task/result files
            with tempfile.TemporaryDirectory() as tmpdir:
                # Set ownership and permissions for container user (UID 1000)
                import os
                os.chown(tmpdir, 1000, 1000)
                os.chmod(tmpdir, 0o755)
                
                task_file = Path(tmpdir) / "task.json"
                result_file = Path(tmpdir) / "result.json"
                
                # Write task data
                from dataclasses import asdict
                with open(task_file, 'w') as f:
                    json.dump(asdict(task), f)
                
                # Docker run command with security limits - mount entire temp dir
                cmd = [
                    "docker", "run",
                    "--rm",
                    "--name", container_name,
                    "--user", "1000:1000",
                    "--memory=6g",
                    "--cpus=2",
                    "--pids-limit=20",
                    "--ulimit", "nofile=64:64",
                    "--ulimit", "fsize=524288000:524288000",
                    "--security-opt", "no-new-privileges",
                    "--network", "none",
                    "-v", f"{tmpdir}:/workspace/shared",
                    "-v", f"{model_path.absolute()}:/workspace/model.zip:ro",
                    self.base_image,
                    "/workspace/shared/task.json",
                    str(uid),
                    "/workspace/model.zip",
                    "/workspace/shared/result.json"
                ]
                
                # Execute with timeout
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=EVAL_TIMEOUT_SEC + 10
                    )
                    
                    if proc.returncode != 0:
                        stderr_str = stderr.decode() if stderr else ""
                        bt.logging.debug(f"Container failed for UID {uid}: {stderr_str[:300]}")
                        # Container failed - return zero score immediately
                        bt.logging.info(f"🏁 Ending Docker container for UID {uid} - evaluation failed")
                        return ValidationResult(uid, False, 0.0, 0.0, 0.0)
                    
                except asyncio.TimeoutError:
                    # Kill container if timeout
                    subprocess.run(["docker", "kill", container_name], capture_output=True)
                    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
                    bt.logging.warning(f"Container timeout for UID {uid}")
                    # ENHANCED LOGGING: Ending Docker for timeout
                    bt.logging.info(f"🏁 Ending Docker container for UID {uid} - evaluation timed out")
                
                # Read results
                if result_file.exists():
                    try:
                        with open(result_file, 'r') as f:
                            data = json.load(f)
                        
                        # Check for fake model detection
                        if data.get('is_fake_model', False):
                            bt.logging.warning(f"🚫 FAKE MODEL DETECTED for UID {uid}: {data.get('fake_reason', 'Unknown')}")
                            bt.logging.debug(f"Inspection results: {data.get('inspection_results', {})}")
                            
                            # Store fake model info for blacklisting
                            self.last_fake_model_info = {
                                'uid': uid,
                                'reason': data.get('fake_reason', 'Unknown'),
                                'inspection_results': data.get('inspection_results', {})
                            }
                            
                            # ENHANCED LOGGING: Ending Docker for fake model
                            bt.logging.info(f"🏁 Ending Docker container for UID {uid} - fake model detected")
                            
                            # Return zero score for fake models
                            return ValidationResult(uid, False, 0.0, 0.0, 0.0)
                        
                        had_error = "error" in data
                        if had_error:
                            bt.logging.debug(f"Evaluation error for UID {uid}: {data['error']}")
                        
                        result_data = {k: v for k, v in data.items() if k not in ["error", "is_fake_model", "fake_reason", "inspection_results"]}
                        
                        # Clear fake model info since this was a legitimate evaluation
                        self.last_fake_model_info = None
                        
                        # Apply reward floor logic
                        if not had_error and float(result_data.get("score", 0.0)) == 0.0:
                            result_data["score"] = 0.01
                        
                        # Validate score range [0,1]
                        score = float(result_data.get("score", 0.0))
                        if not (0.0 <= score <= 1.0):
                            bt.logging.error(f"🚫 Invalid score {score} for UID {uid} - blacklisting model")
                            from swarm.utils.hash import sha256sum
                            from swarm.core.Model_verify import add_to_blacklist
                            model_hash = sha256sum(model_path)
                            add_to_blacklist(model_hash)
                            return ValidationResult(uid, False, 0.0, 0.0, 0.0)
                        
                        # Log result data exactly as requested - custom format with emoji
                        from datetime import datetime
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"{timestamp} 🔍 DEBUG: UID {uid} result_data: {result_data}")
                        
                        # ENHANCED LOGGING: Successfully ending Docker for UID
                        bt.logging.info(f"🏁 Ending Docker container for UID {uid} - evaluation completed successfully")
                        
                        return ValidationResult(**result_data)
                        
                    except Exception as e:
                        bt.logging.warning(f"Failed to parse result for UID {uid}: {e}")
                        # ENHANCED LOGGING: Ending Docker for result parsing error
                        bt.logging.info(f"🏁 Ending Docker container for UID {uid} - result parsing failed")
                else:
                    bt.logging.warning(f"No result file found for UID {uid}")
                    # ENHANCED LOGGING: Ending Docker for missing results
                    bt.logging.info(f"🏁 Ending Docker container for UID {uid} - no results generated")
        
        except Exception as e:
            bt.logging.warning(f"Docker evaluation failed for UID {uid}: {e}")
            # Ensure container is killed
            subprocess.run(["docker", "kill", container_name], capture_output=True)
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            # ENHANCED LOGGING: Ending Docker for general error
            bt.logging.info(f"🏁 Ending Docker container for UID {uid} - evaluation failed with error")
        finally:
            # Best-effort ensure container is removed even if --rm didn't trigger
            try:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            except Exception:
                pass
            # Periodically prune dangling images/caches to keep disk usage low
            # IMPORTANT: Only prune dangling images, NOT all unused images (removed -a flag)
            # This preserves the base image between evaluations
            try:
                subprocess.run(["docker", "image", "prune", "-f"], capture_output=True)
                subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True)
            except Exception:
                pass
        
        # ENHANCED LOGGING: Final fallback ending message
        bt.logging.info(f"🏁 Ending Docker container for UID {uid} - returning default result")
        return ValidationResult(uid, False, 0.0, 0.0, 0.0)
    
    
    def cleanup(self):
        """Clean up any orphaned containers and prune unused images/cache"""
        try:
            # List all swarm evaluation containers
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=swarm_eval_", "--format", "{{.Names}}"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout:
                containers = result.stdout.strip().split('\n')
                for container in containers:
                    if container:
                        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
                        bt.logging.debug(f"Cleaned up orphaned container: {container}")

            # Also clean up verification containers
            result_verify = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=swarm_verify_", "--format", "{{.Names}}"],
                capture_output=True,
                text=True
            )
            if result_verify.returncode == 0 and result_verify.stdout:
                containers_v = result_verify.stdout.strip().split('\n')
                for container in containers_v:
                    if container:
                        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
                        bt.logging.debug(f"Cleaned up orphaned verify container: {container}")

            # Prune only dangling images and builder cache to reclaim disk space
            # IMPORTANT: Only prune dangling images, NOT all unused images (removed -a flag)
            # This preserves the base image
            subprocess.run(["docker", "image", "prune", "-f"], capture_output=True)
            subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True)
            subprocess.run(["docker", "volume", "prune", "-f"], capture_output=True)
        
        except Exception as e:
            bt.logging.warning(f"Container cleanup failed: {e}")

