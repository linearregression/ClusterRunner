import os
import shutil
from urllib.parse import urlparse

from app.project_type.project_type import ProjectType
from app.util import fs, log
from app.util.conf.configuration import Configuration


class Git(ProjectType):
    """
    Example API call to invoke a git-type build.
    {
        "type": "git",
        "url": "https://github.com/box/StatusWolf.git",
    }
    """
    CLONE_DEPTH = 50
    DIRECTORY_PERMISSIONS = 0o700

    @staticmethod
    def get_full_repo_directory(url):
        """
        Generates a directory to house the repo based on the origin url
        :return: A path to clone the git repo in
        :rtype: str
        """
        url_components = urlparse(url)
        url_full_path_parts = url_components.path.split('/')
        repo_name = url_full_path_parts[-1].split('.')[0]
        url_folder_path_parts = url_full_path_parts[:-1]
        repo_directory = os.path.join(Configuration['repo_directory'], url_components.netloc, *url_folder_path_parts)
        return fs.remove_invalid_path_characters(os.path.join(repo_directory, repo_name))

    @staticmethod
    def get_timing_file_directory(url):
        """
        Generates the path to store timing results in
        :param url: The remote 'origin' url for the git repo
        :return: A path for storing timing files
        :rtype: str
        """
        url_components = urlparse(url)
        timings_directory = os.path.join(
            Configuration['timings_directory'],
            url_components.netloc,
            url_components.path.strip('/')
        )
        return fs.remove_invalid_path_characters(timings_directory)

    # pylint: disable=redefined-builtin
    # Disable "redefined-builtin" because renaming the "hash" parameter would be a breaking change.
    # todo: Deprecate the "branch" parameter and create a new one named "ref" to replace it.
    def __init__(self, url, build_project_directory='', project_directory='', remote='origin', branch='master',
                 hash='FETCH_HEAD', config=None, job_name=None, remote_files=None):
        """
        Note: the first line of each parameter docstring will be exposed as command line argument documentation for the
        clusterrunner build client.

        :param url: url to the git repo (ie: https, ssh)
        :type url: str
        :param build_project_directory: the symlinked directory of where PROJECT_DIR should end up being set to
        :type build_project_directory: str
        :param project_directory: path within the repo that contains cluster_runner.yaml
        :type project_directory: str
        :param remote: The git remote name to fetch from
        :type remote: str
        :param branch: The git branch name on the remote to fetch
        :type branch: str
        :param hash: The hash to reset hard on. If hash is not set, we use the FETCH_HEAD of <branch>.
        :type hash: str
        :param config: a yaml string representing the project_type's config
        :type config: str|None
        :param job_name: a list of job names we intend to run
        :type job_name: list [str] | None
        :param remote_files: dictionary mapping of output file to URL
        :type remote_files: dict[str, str] | None
        """
        super().__init__(config, job_name, remote_files)
        self._url = url
        self._remote = remote
        self._branch = branch
        self._hash = hash
        self._repo_directory = self.get_full_repo_directory(self._url)
        self._timing_file_directory = self.get_timing_file_directory(self._url)
        self._local_ref = None
        self._logger = log.get_logger(__name__)

        # We explicitly set the repo directory to 700 so we don't inadvertently expose the repo to access by other users
        fs.create_dir(self._repo_directory, self.DIRECTORY_PERMISSIONS)
        fs.create_dir(self._timing_file_directory, self.DIRECTORY_PERMISSIONS)
        fs.create_dir(os.path.dirname(build_project_directory))

        # Create a symlink from the generated build project directory to the actual project directory.
        # This is done in order to switch between the master's and the slave's copies of the repo while not
        # having to do something hacky in order to user the master's generated atoms on the slaves.
        actual_project_directory = os.path.join(self._repo_directory, project_directory)
        try:
            os.unlink(build_project_directory)
        except FileNotFoundError:
            pass

        os.symlink(actual_project_directory, build_project_directory)
        self.project_directory = build_project_directory

    def slave_param_overrides(self):
        """
        Produce a set of values to override original project type params for use on a slave machine.

        :return: A set of values to override original project type params
        :rtype: dict[str, str]
        """
        param_overrides = super().slave_param_overrides()

        # We modify the repo url so the slave clones or fetches from the master directly. This should be faster than
        # cloning/fetching from the original git remote.
        master_repo_url = 'ssh://{}{}'.format(Configuration['hostname'], self._repo_directory)
        param_overrides['url'] = master_repo_url  # This causes the slave to clone directly from the master.

        # The user-specified branch is overwritten with a locally created ref so that slaves working on a job can
        # continue to fetch the same HEAD, even if the master resets the user-specified branch for another build.
        param_overrides['branch'] = self._local_ref

        return param_overrides

    def _fetch_project(self):
        """
        Clones the project if necessary, fetches from the remote repo and resets to the requested commit
        """
        # For backward compatibility: If a shallow repo exists, delete it.  Shallow cloning is no longer supported,
        # it causes failures when fetching refs that depend on commits which are excluded from the shallow clone.
        existing_repo_is_shallow = os.path.isfile(os.path.join(self._repo_directory, '.git', 'shallow'))
        if existing_repo_is_shallow:
            if os.path.exists(self._repo_directory):
                shutil.rmtree(self._repo_directory)
                fs.create_dir(self._repo_directory, self.DIRECTORY_PERMISSIONS)

        # Clone the repo if it doesn't exist
        try:
            self._execute_git_command_in_repo_and_raise_on_failure('rev-parse')  # rev-parse succeeds if repo exists
        except RuntimeError:
            self._logger.notice('No valid repo in "{}". Cloning fresh from "{}".', self._repo_directory, self._url)
            self._execute_git_command_in_repo_and_raise_on_failure(
                git_command='clone {} {}'. format(self._url, self._repo_directory),
                error_msg='Could not clone repo.'
            )

        # Must add the --update-head-ok in the scenario that the current branch of the working directory
        # is equal to self._branch, otherwise the git fetch will exit with a non-zero exit code.
        self._execute_git_command_in_repo_and_raise_on_failure(
            git_command='fetch --update-head-ok {} {}'.format(self._remote, self._branch),
            error_msg='Could not fetch specified branch "{}" from remote "{}".'.format(self._branch, self._remote)
        )

        # Validate and convert the user-specified hash/refspec to a full git hash
        self._hash = self._execute_git_command_in_repo_and_raise_on_failure(
            git_command='rev-parse {}'.format(self._hash),
            error_msg='Could not rev-parse "{}" to a commit hash.'.format(self._hash)
        ).strip()

        # Save this hash as a local ref. Named local refs are necessary for slaves to fetch correctly from the master.
        # The local ref will be passed on to slaves instead of the user-specified branch.
        self._local_ref = 'refs/clusterrunner/' + self._hash
        self._execute_git_command_in_repo_and_raise_on_failure(
            git_command='update-ref {} {}'.format(self._local_ref, self._hash),
            error_msg='Could not update local ref.'
        )

        # The '--' argument acts as a delimiter to differentiate values that can be "tree-ish" or a "path"
        self._execute_git_command_in_repo_and_raise_on_failure(
            git_command='reset --hard {} --'.format(self._hash),
            error_msg='Could not reset Git repo.'
        )

        self._execute_git_command_in_repo_and_raise_on_failure(
            git_command='clean -dfx',
            error_msg='Could not clean Git repo.'
        )

    def _execute_git_command_in_repo_and_raise_on_failure(self, git_command, error_msg='Error executing git command.'):
        """
        Execute the given git command. If it exits with a failing exit code then raise an exception.

        We also set some environment variables (e.g., GIT_SSH, GIT_ASKPASS) that should prevent git from trying to
        display an interactive prompt.

        :param git_command: The git command to execute, e.g., "fetch origin"
        :type git_command: string
        :param error_msg: The human readable error message to log if the command fails
        :type error_msg: string
        :return: The output of the process (stdout and stderr)
        :rtype: string
        """
        # The option that prevents ssh from displaying interactive prompts is "BatchMode=yes".
        strict_host_key_setting = 'yes' if Configuration['git_strict_host_key_checking'] else 'no'
        git_ssh_args = '-o BatchMode=yes -o StrictHostKeyChecking={}'.format(strict_host_key_setting)

        env_vars = {
            'GIT_ASKPASS': Configuration['git_askpass_exe'],
            'GIT_SSH': Configuration['git_ssh_exe'],
            'GIT_SSH_ARGS': git_ssh_args,  # GIT_SSH_ARGS is not used by git; it is used by our git_ssh.sh wrapper.
        }
        command = 'git ' + git_command
        return self._execute_and_raise_on_failure(command, error_msg, cwd=self._repo_directory, env_vars=env_vars)

    def execute_command_in_project(self, *args, **kwargs):
        """
        Execute a command inside the repo. See superclass for parameter documentation.

        :return: A 2-tuple of: (the process output/error, the process exit code)
        :rtype: (string, int)
        """
        # There is a scenario where self.project_directory doesn't exist yet (when a certain repo has never been
        # fetched before on this particular machine). In order to avoid having python barf during this scenario,
        # we have only pass in the cwd if it exists.
        if 'cwd' not in kwargs:
            kwargs['cwd'] = self.project_directory if os.path.exists(self.project_directory) else None
        return super().execute_command_in_project(*args, **kwargs)

    def timing_file_path(self, job_name):
        """
        :type job_name: str
        :return: the absolute path to where the timing file for job_name SHOULD be. This method does not guarantee
            that the timing file exists.
        :rtype: string
        """
        return os.path.join(self._timing_file_directory, "{}.timing.json".format(job_name))

    def project_id(self):
        return self._repo_directory
