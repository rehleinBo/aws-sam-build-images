from unittest import TestCase
from pathlib import Path
import os
import tarfile
import docker  # type: ignore
import pytest
import tempfile
import subprocess


# These are the runtimes which doesn't have hello-world template, skipping them
# for running `sam build -u` tests
SKIP_CONTAINERIZED_BUILD_TESTS = {"provided", "provided.al2", "provided.al2023", "dotnet7"}
# These are the runtimes which requires `--mount-with WRITE` option to build functions
# in a containerized build
MOUNT_WITH_WRITE_RUNTIMES = {"dotnet6"}


class BuildImageBase(TestCase):
    __test__ = False
    package_managers = ["yum"]

    @classmethod
    def setUpClass(cls, runtime, dockerfile, dep_manager=None, tag="x86_64"):
        """
        Test setup for each build image

        :param runtime: runtime of the build image
        :param dockerfile: dockerfile name of the build image
        :param dep_manager: dependency manager of the build image
        :param tag: architecture for the build image, used as an image tag
        """
        cls.image = f"amazon/aws-sam-cli-build-image-{runtime}:{tag}"
        cls.tag = tag
        cls.app_location = f"tests/apps/{runtime}"
        cls.runtime = runtime
        cls.dep_manager = dep_manager
        cls.client = docker.from_env(timeout=300)
        cls.sam_version = os.getenv("SAM_CLI_VERSION")
        TestCase().assertTrue(
            cls.sam_version
        )  # check if SAM_CLI_VERSION env variable is set

        cls.docker_image = None
        try:  # check if the image exists, else build one
            cls.client.images.get(cls.image)
        except docker.errors.ImageNotFound:
            cls.docker_image = cls.client.images.build(
                path="build-image-src/",
                dockerfile=dockerfile,
                tag=cls.image,
                buildargs={
                    "SAM_CLI_VERSION": cls.sam_version,
                    "IMAGE_ARCH": tag,
                },
            )

    @classmethod
    def tearDownClass(cls):
        """
        Cleanup after testing the build image.
        Removes all stopped containers and images built by the setUpClass.
        """
        cls.client.containers.prune()
        if cls.docker_image:
            cls.client.images.remove(image=cls.image, force=True)
            cls.client.images.remove(
                image=f"amazon/aws-sam-cli-emulation-image-{cls.runtime}", force=True
            )

        cls.client.images.prune()

    def test_common_packages(self):
        """
        Test common packages present in all build images
        """
        self.assertTrue(
            self.check_package_output(
                "sam --version", f"SAM CLI, version {self.sam_version}"
            )
        )
        self.assertTrue(self.is_package_present("aws"))
        self.assertTrue(self.is_package_present("jq"))
        for pm in self.package_managers:
            self.assertTrue(self.is_package_present(pm))

    def test_sam_init(self):
        """
        Test sam init hello world application for the given runtime and dependency manager
        """
        if self.runtime in ["provided", "provided.al2", "provided.al2023", "dotnet7"]:
            pytest.skip("Skipping sam init test for self-provided images")

        sam_init = f"sam init \
--name sam-app \
--runtime {self.runtime} \
--package-type Zip \
--app-template hello-world"
        if self.dep_manager:
            sam_init += f" --dependency-manager {self.dep_manager}"

        # For nodejs20.x set LD_LIBRARY_PATH env variable to execute sam commands
        if self.runtime == 'nodejs20.x':
            op = self.client.containers.run(
                image=self.image,
                command=[
                    "/bin/sh",
                    "-c",
                    sam_init + " && cd sam-app && sam build",
                ],
                environment=["LD_LIBRARY_PATH="]
            ).decode()
        else:
            op = self.client.containers.run(
                image=self.image,
                command=[
                    "/bin/sh",
                    "-c",
                    sam_init + " && cd sam-app && sam build",
                ],
            ).decode()
        self.assertTrue(op.find("Build Succeeded"))

    def test_external_apps(self):
        """
        BYOApps for testing inside the build image. Place your apps in the tests/buildimages/apps/{runtime} folder
        """
        apps = []
        try:
            _, apps, _ = next(
                os.walk(self.app_location)
            )  # Get all directories one level below the app location
        except StopIteration:  # When no apps are present in the app location
            pytest.skip("No external apps found for testing.")

        for app in apps:
            # For each app, check if app contains template.yaml file, make a tarball of the app directory,
            # start a container, extract the tarball in the container and build the app
            app_path = Path().resolve().joinpath(self.app_location, app)
            if app_path.joinpath("template.yaml").is_file():
                with tarfile.open(app + ".tar", "w") as tar:
                    tar.add(app_path, app)

                tar_data = open(app + ".tar", "rb").read()

                container = self.client.containers.run(
                    self.image, "/bin/bash", detach=True, tty=True
                )
                container.put_archive("/var/task", tar_data)
                _, out = container.exec_run("sam build", workdir="/var/task/" + app)
                os.remove(app + ".tar")
                container.kill()

                self.assertTrue(out.decode().find("Build Succeeded"))
    
    def test_containerized_build(self):
        if self.runtime in SKIP_CONTAINERIZED_BUILD_TESTS:
            self.skipTest(f"Skipping for {self.runtime}")
        init_args = [
            "sam", 
            "init", 
            "--no-interactive", 
            "--runtime",  self.runtime, 
            "--app-template", "hello-world", 
            "--name", "sam-app", 
            "--dependency-manager", self.dep_manager, 
            "--architecture", self.tag
        ]
        build_args = [
            "sam", "build", "--use-container", "--build-image", self.image
        ]
        # add --mount-with WRITE option for dotnet runtimes
        if self.runtime in MOUNT_WITH_WRITE_RUNTIMES:
            build_args += ["--mount-with", "WRITE"]
        invoke_args = [
            "sam", "local", "invoke", "HelloWorldFunction"
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            init_result = subprocess.run(init_args, cwd=tmpdir)
            self.assertEqual(init_result.returncode, 0)

            build_result = subprocess.run(build_args, cwd=os.path.join(tmpdir, "sam-app"))
            self.assertEqual(build_result.returncode, 0)

            invoke_result = subprocess.run(invoke_args, cwd=os.path.join(tmpdir, "sam-app"))
            self.assertEqual(invoke_result.returncode, 0)

    def is_package_present(self, package):
        """
        Helper function to check if a package is present in the image
        """
        try:
            self.client.containers.run(
                self.image, command=["/bin/sh", "-c", f"command -v {package}"]
            )
            return True
        except docker.errors.ContainerError:
            return False

    def check_package_output(self, pkg_cmd, output, std_err=False):
        """
        Helper function to check a package's actual output contains the given expected substring
        """
        return (
            output
            in self.client.containers.run(self.image, pkg_cmd, stderr=std_err)
            .decode()
            .strip()
        )

    def is_architecture(self, architecture):
        """
        Returns a value indicating whether the current container is of the specified architecture or not

        Parameters
        ----------
        architecture : str
            Architecture

        Returns
        -------
        bool
            True if the container is of the architecture, False otherwise
        """

        result = str(
            self.client.containers.run(self.image, command=["/bin/uname", "-m"])
        )
        return architecture in result

class AL2023BasedBuildImageBase(BuildImageBase):
    package_managers = ["dnf"]

