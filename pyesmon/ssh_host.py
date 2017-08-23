# pylint: disable=too-many-lines
# Copyright (c) 2017 DataDirect Networks, Inc.
# All Rights Reserved.
# Author: lixi@ddn.com
"""
The host that localhost could use SSH to run command
"""

import time
import logging
import os
import glob
import shutil
import re

# local libs
from pyesmon import utils


# OS distribution RHEL6/CentOS6
DISTRO_RHEL6 = "rhel6"
# OS distribution RHEL7/CentOS7
DISTRO_RHEL7 = "rhel7"
# The shortest time that a reboot could finish. It is used to check whether
# a host has actually rebooted or not.
SHORTEST_TIME_REBOOT = 10
# The logest time that a reboot wil takes
LONGEST_TIME_REBOOT = 240
# The longest time that a simple command should finish
LONGEST_SIMPLE_COMMAND_TIME = 600
# Yum install is slow, so use a larger timeout value
LONGEST_TIME_YUM_INSTALL = LONGEST_SIMPLE_COMMAND_TIME * 2
# RPM install is slow, so use a larger timeout value
LONGEST_TIME_RPM_INSTALL = LONGEST_SIMPLE_COMMAND_TIME * 2


def sh_escape(command):
    """
    Escape special characters from a command so that it can be passed
    as a double quoted (" ") string in a (ba)sh command.

    Args:
            command: the command string to escape.

    Returns:
            The escaped command string. The required englobing double
            quotes are NOT added and so should be added at some point by
            the caller.

    See also: http://www.tldp.org/LDP/abs/html/escapingsection.html
    """
    command = command.replace("\\", "\\\\")
    command = command.replace("$", r'\$')
    command = command.replace('"', r'\"')
    command = command.replace('`', r'\`')
    return command


def scp_remote_escape(filename):
    """
    Escape special characters from a filename so that it can be passed
    to scp (within double quotes) as a remote file.

    Bis-quoting has to be used with scp for remote files, "bis-quoting"
    as in quoting x 2
    scp does not support a newline in the filename

    Args:
            filename: the filename string to escape.

    Returns:
            The escaped filename string. The required englobing double
            quotes are NOT added and so should be added at some point by
            the caller.
    """
    escape_chars = r' !"$&' "'" r'()*,:;<=>?[\]^`{|}'

    new_name = []
    for char in filename:
        if char in escape_chars:
            new_name.append("\\%s" % (char,))
        else:
            new_name.append(char)

    return sh_escape("".join(new_name))


def make_ssh_command(login_name="root", identity_file=None):
    """
    Return the ssh cmd string
    """
    extra_option = ""
    if identity_file is not None:
        extra_option = ("-i %s" % identity_file)
    full_command = ("ssh -a -x -l %s -o StrictHostKeyChecking=no "
                    "-o BatchMode=yes %s" %
                    (login_name, extra_option))
    return full_command


def ssh_command(hostname, command, login_name="root", identity_file=None):
    """
    Return the ssh command on a remote host
    """
    ssh_string = make_ssh_command(login_name=login_name,
                                  identity_file=identity_file)
    full_command = ("%s %s \"%s\"" %
                    (ssh_string, hostname, sh_escape(command)))
    return full_command


def ssh_run(hostname, command, login_name="root", timeout=None,
            stdout_tee=None, stderr_tee=None, stdin=None,
            return_stdout=True, return_stderr=True,
            quit_func=None, identity_file=None, flush_tee=False):
    """
    Use ssh to run command on a remote host
    """
    # pylint: disable=too-many-arguments
    full_command = ssh_command(hostname, command, login_name, identity_file)
    return utils.run(full_command, timeout=timeout, stdout_tee=stdout_tee,
                     stderr_tee=stderr_tee, stdin=stdin,
                     return_stdout=return_stdout, return_stderr=return_stderr,
                     quit_func=quit_func, flush_tee=flush_tee)


class SSHHost(object):
    """
    Each SSH host has an object of SSHHost
    """
    # pylint: disable=too-many-public-methods
    def __init__(self, hostname, identity_file=None, local=False):
        self.sh_hostname = hostname
        self.sh_identity_file = identity_file
        self.sh_local = local

    def sh_is_up(self, timeout=60):
        """
        Whether this host is up now
        """
        ret = self.sh_run("true", timeout=timeout)
        if ret.cr_exit_status != 0:
            return False
        return True

    def sh_expect_retval(self, retval, args):
        """
        Return 0 if got expected retval
        """
        # pylint: disable=no-self-use
        expect_exit_status = args[0]
        expect_stdout = args[1]
        expect_stderr = args[2]
        if (expect_exit_status is not None and
                expect_exit_status != retval.cr_exit_status):
            return -1

        if (expect_stdout is not None and
                expect_stdout != retval.cr_stdout):
            return -1

        if (expect_stderr is not None and
                expect_stderr != retval.cr_stderr):
            return -1
        return 0

    def sh_wait_condition(self, command, condition_func, args, timeout=90,
                          sleep_interval=1):
        # pylint: disable=too-many-arguments
        """
        Wait until the condition_func returns 0
        """
        waited = 0
        while True:
            retval = self.sh_run(command)
            ret = condition_func(retval, args)
            if ret:
                if waited < timeout:
                    waited += sleep_interval
                    time.sleep(sleep_interval)
                    continue
                logging.error("timeout on host [%s], "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              self.sh_hostname,
                              retval.cr_exit_status,
                              retval.cr_stdout,
                              retval.cr_stderr)
                return -1
            return 0
        return -1

    def sh_wait_update(self, command, expect_exit_status=None,
                       expect_stdout=None,
                       expect_stderr=None,
                       timeout=90,
                       sleep_interval=1):
        # pylint: disable=too-many-arguments
        """
        Wait until the command result on a host changed to expected values
        """
        args = [expect_exit_status, expect_stdout, expect_stderr]
        return self.sh_wait_condition(command, self.sh_expect_retval,
                                      args, timeout=timeout,
                                      sleep_interval=sleep_interval)

    def sh_wait_up(self, timeout=LONGEST_TIME_REBOOT):
        """
        Wait until the host is up
        """
        return self.sh_wait_update("true", expect_exit_status=0,
                                   timeout=timeout)

    def sh_distro(self):
        """
        Return the distro of this host
        """
        # pylint: disable=too-many-return-statements,too-many-branches
        ret = self.sh_run("yum install redhat-lsb -y",
                          timeout=LONGEST_TIME_YUM_INSTALL)
        if ret.cr_exit_status != 0:
            logging.error("failed to install redhat-lsb on host [%s]",
                          self.sh_hostname)
            return None

        ret = self.sh_run("which lsb_release")
        if ret.cr_exit_status != 0:
            logging.error("lsb_release is needed on host [%s] for accurate "
                          "distro identification", self.sh_hostname)
            return None

        ret = self.sh_run("lsb_release -s -i")
        if ret.cr_exit_status != 0:
            logging.error("failed to distro name on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          self.sh_hostname, ret.cr_exit_status,
                          ret.cr_stdout, ret.cr_stderr)
            return None
        name = ret.cr_stdout.strip('\n')

        ret = self.sh_run("lsb_release -s -r")
        if ret.cr_exit_status != 0:
            logging.error("failed to distro version on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          self.sh_hostname, ret.cr_exit_status,
                          ret.cr_stdout, ret.cr_stderr)
            return None
        version = ret.cr_stdout.strip('\n')

        if (name == "RedHatEnterpriseServer" or
                name == "ScientificSL" or
                name == "CentOS"):
            if version.startswith("7"):
                return DISTRO_RHEL7
            elif version.startswith("6"):
                return DISTRO_RHEL6
            else:
                logging.error("unsupported version [%s] of [%s] on host [%s]",
                              version, "rhel", self.sh_hostname)
                return None
        elif name == "EnterpriseEnterpriseServer":
            logging.error("unsupported version [%s] of [%s] on host [%s]",
                          version, "oel", self.sh_hostname)
            return None
        elif name == "SUSE LINUX":
            # PATCHLEVEL=$(sed -n -e 's/^PATCHLEVEL = //p' /etc/SuSE-release)
            # version="${version}.$PATCHLEVEL"
            logging.error("unsupported version [%s] of [%s] on host [%s]",
                          version, "sles", self.sh_hostname)
            return None
        elif name == "Fedora":
            logging.error("unsupported version [%s] of [%s] on host [%s]",
                          version, "fc", self.sh_hostname)
            return None
        else:
            logging.error("unsupported version [%s] of [%s] on host [%s]",
                          version, name, self.sh_hostname)
            return None

    def sh_prepare_user(self, name, uid, gid):
        """
        Add an user if it doesn't exist
        """
        # pylint: disable=too-many-return-statements
        ret = self.sh_run("grep '%s:%s' /etc/passwd | wc -l" % (uid, gid))
        if ret.cr_exit_status != 0:
            logging.warning("failed to check uid [%s] gid [%s] on host "
                            "[%s], ret = [%d], stdout = [%s], stderr = [%s]",
                            uid, gid, self.sh_hostname, ret.cr_exit_status,
                            ret.cr_stdout, ret.cr_stderr)
            return -1

        if ret.cr_stdout.strip() != "0":
            logging.debug("user [%s] with uid [%s] gid [%s] already exists "
                          "on host [%s], will not create it",
                          name, uid, gid, self.sh_hostname)
            return 0

        ret = self.sh_run("getent group %s" % (gid))
        if ret.cr_exit_status != 0 and len(ret.cr_stdout.strip()) != 0:
            logging.warning("failed to check gid [%s] on host "
                            "[%s], ret = [%d], stdout = [%s], stderr = [%s]",
                            gid, self.sh_hostname, ret.cr_exit_status,
                            ret.cr_stdout, ret.cr_stderr)
            return -1

        if ret.cr_stdout.strip() == "0":
            ret = self.sh_run("groupadd -g %s %s" % (gid, name))
            if ret.cr_exit_status != 0:
                logging.warning("failed to add group [%s] with gid [%s] on "
                                "host [%s], ret = [%d], stdout = [%s], "
                                "stderr = [%s]",
                                name, gid, self.sh_hostname,
                                ret.cr_exit_status,
                                ret.cr_stdout,
                                ret.cr_stderr)
                return -1
        else:
            logging.debug("group [%s] with gid [%s] already exists on "
                          "host [%s], will not create it",
                          name, gid, self.sh_hostname)
            return 0

        ret = self.sh_run("useradd -u %s -g %s %s" % (uid, gid, name))
        if ret.cr_exit_status != 0:
            logging.warning("failed to add user [%s] with uid [%s] gid [%s] "
                            "on host [%s], ret = [%d], stdout = [%s], "
                            "stderr = [%s]",
                            name, uid, gid, self.sh_hostname,
                            ret.cr_exit_status, ret.cr_stdout, ret.cr_stderr)
            return -1
        return 0

    def sh_umount(self, device):
        """
        Umount the file system of a device
        """
        retval = self.sh_run("umount %s" % device)
        if retval.cr_exit_status != 0:
            logging.error("failed to run [umount %s] on host [%s], "
                          "will try with -f again",
                          device, self.sh_hostname)
            retval = self.sh_run("umount -f %s" % device)
            if retval.cr_exit_status != 0:
                logging.error("failed to run [umount -f %s] on host [%s]",
                              device, self.sh_hostname)
                return -1
        return 0

    def sh_lustre_umount(self):
        """
        Umount all file systems of Lustre
        """
        retval = self.sh_run("mount | grep 'type lustre' | awk '{print $1}'")
        if retval.cr_exit_status != 0:
            logging.error("failed to get lustre mount points on host "
                          "[%s]",
                          self.sh_hostname)
            return -1

        ret = 0
        devices = retval.cr_stdout.splitlines()
        # Umount client first, so as to prevent dependency
        for device in devices[:]:
            if device.startswith("/dev"):
                continue
            ret = self.sh_umount(device)
            if ret:
                break
            devices.remove(device)
        if ret == 0:
            for device in devices:
                ret = self.sh_umount(device)
                if ret:
                    break
        return ret

    def sh_get_uptime(self):
        """
        Get the uptime of the host
        """
        command = ("expr $(date +%s) - $(cat /proc/uptime | "
                   "awk -F . '{print $1}')")
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("can't get uptime on host [%s], command = [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          self.sh_hostname, command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1
        return int(retval.cr_stdout)

    def sh_umount_nfs(self, server_name, nfs_path, mnt_path):
        """
        Umount a NFS client on the host
        """
        logging.info("umounting NFS client of [%s:%s] from [%s] on host [%s]",
                     server_name, nfs_path, mnt_path, self.sh_hostname)
        nfs_commands = [("umount %s" % (mnt_path))]
        for command in nfs_commands:
            retval = self.sh_run(command)
            if retval.cr_exit_status != 0:
                logging.error("failed to run command [%s] on host [%s]",
                              command, self.sh_hostname)
                return -1
        logging.info("umounted NFS client of [%s:%s] from [%s] on host [%s]",
                     server_name, nfs_path, mnt_path, self.sh_hostname)
        return 0

    def sh_export_nfs(self, nfs_path):
        """
        Export NFS server on the host
        """
        logging.info("exporting nfs directory [%s] on host [%s]",
                     nfs_path, self.sh_hostname)
        nfs_commands = ["service nfs start", "mount | grep nfsd",
                        ("exportfs -o rw,no_root_squash *:%s" % nfs_path),
                        ("exportfs | grep %s" % nfs_path)]
        for command in nfs_commands:
            retval = self.sh_run(command)
            if retval.cr_exit_status != 0:
                logging.error("failed to run command [%s] on host [%s]",
                              command, self.sh_hostname)
                return -1
        logging.info("exported nfs directory [%s] on host [%s]",
                     nfs_path, self.sh_hostname)
        return 0

    def sh_mount_nfs(self, server_name, nfs_path, mnt_path):
        """
        Mount a NFS client on the host
        """
        logging.info("mounting NFS client of [%s:%s] to [%s] on host [%s]",
                     server_name, nfs_path, mnt_path, self.sh_hostname)
        nfs_commands = [("umount %s || echo -n """ % mnt_path),
                        ("ping -c 1 %s" % server_name),
                        ("test -e %s || mkdir -p %s" % (mnt_path, mnt_path)),
                        ("test -d %s" % mnt_path),
                        ("mount -t nfs %s:%s %s" %
                         (server_name, nfs_path, mnt_path)),
                        ("mount | grep %s:%s" % (server_name, nfs_path))]
        for command in nfs_commands:
            retval = self.sh_run(command)
            if retval.cr_exit_status != 0:
                logging.error("failed to run command [%s] on host [%s], "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              command, self.sh_hostname,
                              retval.cr_exit_status,
                              retval.cr_stdout, retval.cr_stderr)
                return -1
        logging.info("mounted NFS client of [%s:%s] to [%s] on host [%s]",
                     server_name, nfs_path, mnt_path, self.sh_hostname)
        return 0

    def sh_make_rsync_compatible_globs(self, path, is_local):
        """
        Given an rsync-style path, returns a list of globbed paths
        that will hopefully provide equivalent behaviour for scp. Does not
        support the full range of rsync pattern matching behaviour, only that
        exposed in the get/send_file interface (trailing slashes).

        The is_local param is flag indicating if the paths should be
        interpreted as local or remote paths.
        """

        # non-trailing slash paths should just work
        if len(path) == 0 or path[-1] != "/":
            return [path]

        # make a function to test if a pattern matches any files
        if is_local:
            def glob_matches_files(path, pattern):
                """
                Match the files on local host
                """
                return len(glob.glob(path + pattern)) > 0
        else:
            def glob_matches_files(path, pattern):
                """
                Match the files on remote host
                """
                result = self.sh_run("ls \"%s\"%s" %
                                     (sh_escape(path), pattern))
                return result.cr_exit_status == 0

        # take a set of globs that cover all files, and see which are needed
        patterns = ["*", ".[!.]*"]
        patterns = [p for p in patterns if glob_matches_files(path, p)]

        # convert them into a set of paths suitable for the commandline
        if is_local:
            return ["\"%s\"%s" % (sh_escape(path), pattern)
                    for pattern in patterns]
        else:
            return [scp_remote_escape(path) + pattern
                    for pattern in patterns]

    def sh_make_scp_cmd(self, sources, dest):
        """
        Given a list of source paths and a destination path, produces the
        appropriate scp command for encoding it. Remote paths must be
        pre-encoded.
        """
        # pylint: disable=no-self-use
        extra_option = ""
        if self.sh_identity_file is not None:
            extra_option = ("-i %s" % self.sh_identity_file)
        command = ("scp -rqp -o StrictHostKeyChecking=no %s "
                   "%s '%s'")
        return command % (extra_option, " ".join(sources), dest)

    def sh_make_rsync_compatible_source(self, source, is_local):
        """
        Applies the same logic as sh_make_rsync_compatible_globs, but
        applies it to an entire list of sources, producing a new list of
        sources, properly quoted.
        """
        return sum((self.sh_make_rsync_compatible_globs(path, is_local)
                    for path in source), [])

    def sh_encode_remote_paths(self, paths, escape=True):
        """
        Given a list of file paths, encodes it as a single remote path, in
        the style used by rsync and scp.
        """
        if escape:
            paths = [scp_remote_escape(path) for path in paths]
        return 'root@%s:"%s"' % (self.sh_hostname, " ".join(paths))

    def sh_set_umask_perms(self, dest):
        """
        Given a destination file/dir (recursively) set the permissions on
        all the files and directories to the max allowed by running umask.

        now this looks strange but I haven't found a way in Python to _just_
        get the umask, apparently the only option is to try to set it
        """
        # pylint: disable=no-self-use
        umask = os.umask(0)
        os.umask(umask)

        max_privs = 0777 & ~umask

        def set_file_privs(filename):
            """
            Set the privileges of a file
            """
            file_stat = os.stat(filename)

            file_privs = max_privs
            # if the original file permissions do not have at least one
            # executable bit then do not set it anywhere
            if not file_stat.st_mode & 0111:
                file_privs &= ~0111

            os.chmod(filename, file_privs)

        # try a bottom-up walk so changes on directory permissions won't cut
        # our access to the files/directories inside it
        for root, dirs, files in os.walk(dest, topdown=False):
            # when setting the privileges we emulate the chmod "X" behaviour
            # that sets to execute only if it is a directory or any of the
            # owner/group/other already has execute right
            for dirname in dirs:
                os.chmod(os.path.join(root, dirname), max_privs)

            for filename in files:
                set_file_privs(os.path.join(root, filename))

        # now set privs for the dest itself
        if os.path.isdir(dest):
            os.chmod(dest, max_privs)
        else:
            set_file_privs(dest)

    def sh_get_file(self, source, dest, delete_dest=False, preserve_perm=True):
        """
        copy the file/dir from the host to local host

        Currently, get file is based on scp. For bettern scalability
        we should improve to rsync.
        scp has no equivalent to --delete, just drop the entire dest dir
        """
        dest = os.path.abspath(dest)
        if isinstance(source, basestring):
            source = [source]

        if delete_dest and os.path.isdir(dest):
            shutil.rmtree(dest)
            os.mkdir(dest)

        remote_source = self.sh_make_rsync_compatible_source(source, False)
        if remote_source:
            # sh_make_rsync_compatible_source() already did the escaping
            remote_source = self.sh_encode_remote_paths(remote_source,
                                                        escape=False)
            local_dest = sh_escape(dest)
            scp = self.sh_make_scp_cmd([remote_source], local_dest)
            ret = utils.run(scp)
            if ret.cr_exit_status != 0:
                logging.error("failed to get file [%s] on host [%s] to "
                              "local directory [%s], command = [%s], "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              source, self.sh_hostname, dest, scp,
                              ret.cr_exit_status, ret.cr_stdout,
                              ret.cr_stderr)
                return -1

        if not preserve_perm:
            # we have no way to tell scp to not try to preserve the
            # permissions so set them after copy instead.
            # for rsync we could use "--no-p --chmod=ugo=rwX" but those
            # options are only in very recent rsync versions
            self.sh_set_umask_perms(dest)
        return 0

    def sh_make_rsync_cmd(self, sources, dest, delete_dest, preserve_symlinks):
        """
        Given a list of source paths and a destination path, produces the
        appropriate rsync command for copying them. Remote paths must be
        pre-encoded.
        """
        # pylint: disable=no-self-use
        ssh_cmd = make_ssh_command(identity_file=self.sh_identity_file)
        if delete_dest:
            delete_flag = "--delete"
        else:
            delete_flag = ""
        if preserve_symlinks:
            symlink_flag = ""
        else:
            symlink_flag = "-L"
        command = "rsync %s %s --timeout=1800 --rsh='%s' -az %s %s"
        return command % (symlink_flag, delete_flag, ssh_cmd,
                          " ".join(sources), dest)

    def sh_send_file(self, source, dest, delete_dest=False,
                     preserve_symlinks=False):
        """
        Send file/dir from local host to remote host
        """
        # pylint: disable=too-many-return-statements,too-many-branches
        # pylint: disable=too-many-statements
        if isinstance(source, basestring):
            source_is_dir = os.path.isdir(source)
            source = [source]
        remote_dest = self.sh_encode_remote_paths([dest], False)

        local_sources = [sh_escape(path) for path in source]
        rsync = self.sh_make_rsync_cmd(local_sources, remote_dest,
                                       delete_dest, preserve_symlinks)
        ret = utils.run(rsync)
        if ret.cr_exit_status != 0:
            if("command not found" not in ret.cr_stderr):
                logging.error("failed to send file [%s] on local host "
                              "to dest [%s] on host [%s] using rsync, "
                              "command = [%s], "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              source, dest, self.sh_hostname, rsync,
                              ret.cr_exit_status, ret.cr_stdout,
                              ret.cr_stderr)
                return -1
        else:
            return 0

        # preserve_symlinks is not supported by scp yet
        if preserve_symlinks:
            return -1

        # scp has no equivalent to --delete, just drop the entire dest dir
        if delete_dest:
            command = "test -x %s" % dest
            dest_exists = False
            ret = self.sh_run(command)
            if ret.cr_stdout == "" and ret.cr_stderr == "":
                if ret.cr_exit_status:
                    dest_exists = False
                else:
                    dest_exists = True
            else:
                logging.error("failed to run command [%s]", command)
                return -1

            dest_is_dir = False
            if dest_exists:
                command = "test -d %s" % dest
                ret = self.sh_run(command)
                if ret.cr_stdout == "" and ret.cr_stderr == "":
                    if ret.cr_exit_status:
                        dest_is_dir = False
                    else:
                        dest_is_dir = True
                else:
                    logging.error("failed to run command [%s]", command)
                    return -1

            # If there is a list of more than one path, destination *has*
            # to be a dir. If there's a single path being transferred and
            # it is a dir, the destination also has to be a dir. Therefore
            # it has to be created on the remote machine in case it doesn't
            # exist, otherwise we will have an scp failure.
            if len(source) > 1 or source_is_dir:
                dest_is_dir = True

            if dest_exists and dest_is_dir:
                command = "rm -rf %s && mkdir %s" % (dest, dest)
                ret = self.sh_run(command)
                if ret.cr_exit_status:
                    logging.error("failed to run command [%s]", command)
                    return -1
            elif (not dest_exists) and dest_is_dir:
                command = "mkdir %s" % dest
                ret = self.sh_run(command)
                if ret.cr_exit_status:
                    logging.error("failed to run command [%s]", command)
                    return -1

        local_sources = self.sh_make_rsync_compatible_source(source, False)
        if local_sources:
            scp = self.sh_make_scp_cmd(local_sources, remote_dest)
            ret = utils.run(scp)
            if ret.cr_exit_status != 0:
                logging.error("failed to send file [%s] on local host "
                              "to dest [%s] on host [%s], command = [%s], "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              source, dest, self.sh_hostname, scp,
                              ret.cr_exit_status, ret.cr_stdout,
                              ret.cr_stderr)
                return -1

        return 0

    def sh_run(self, command, silent=False, login_name="root",
               timeout=LONGEST_SIMPLE_COMMAND_TIME, stdout_tee=None,
               stderr_tee=None, stdin=None, return_stdout=True,
               return_stderr=True, quit_func=None, flush_tee=False):
        """
        Run a command on the host
        """
        # pylint: disable=too-many-arguments
        if self.sh_local:
            ret = utils.run(command, timeout=timeout, stdout_tee=stdout_tee,
                            stderr_tee=stderr_tee, stdin=stdin,
                            return_stdout=return_stdout,
                            return_stderr=return_stderr,
                            quit_func=quit_func, flush_tee=flush_tee)
        else:
            ret = ssh_run(self.sh_hostname, command, login_name=login_name,
                          timeout=timeout,
                          stdout_tee=stdout_tee, stderr_tee=stderr_tee,
                          stdin=stdin, return_stdout=return_stdout,
                          return_stderr=return_stderr, quit_func=quit_func,
                          identity_file=self.sh_identity_file,
                          flush_tee=flush_tee)
        if not silent:
            logging.debug("ran [%s] on host [%s], ret = [%d], stdout = [%s], "
                          "stderr = [%s]",
                          command, self.sh_hostname, ret.cr_exit_status,
                          ret.cr_stdout, ret.cr_stderr)
        return ret

    def sh_get_kernel_ver(self):
        """
        Get the kernel version of the remote machine
        """
        ret = self.sh_run("/bin/uname -r")
        if ret.cr_exit_status != 0:
            return None
        return ret.cr_stdout.rstrip()

    def sh_kernel_has_rpm(self):
        """
        Check whether the current running kernel has RPM installed, if not,
        means the RPM has been uninstalled.
        """
        kernel_version = self.sh_get_kernel_ver()
        if kernel_version is None:
            return False

        rpm_name = "kernel-" + kernel_version
        command = "rpm -qi %s" % rpm_name
        retval = self.sh_run(command)
        has_rpm = True
        if retval.cr_exit_status:
            has_rpm = False
        return has_rpm

    def sh_rpm_find_and_uninstall(self, find_command, option=""):
        """
        Find and uninstall RPM on the host
        """
        command = "rpm -qa | %s" % find_command
        retval = self.sh_run(command)
        if retval.cr_exit_status == 0:
            for rpm in retval.cr_stdout.splitlines():
                logging.info("uninstalling RPM [%s] on host [%s]",
                             rpm, self.sh_hostname)
                ret = self.sh_run("rpm -e %s --nodeps %s" % (rpm, option))
                if ret.cr_exit_status != 0:
                    logging.error("failed to uninstall RPM [%s] on host [%s], "
                                  "ret = %d, stdout = [%s], stderr = [%s]",
                                  rpm, self.sh_hostname,
                                  ret.cr_exit_status, ret.cr_stdout,
                                  ret.cr_stderr)
                    return -1
        elif (retval.cr_exit_status == 1 and
              len(retval.cr_stdout) == 0 and
              len(retval.cr_stderr) == 0):
            logging.debug("no rpm can be find by command [%s] on host [%s], "
                          "no need to uninstall",
                          command, self.sh_hostname)
        else:
            return -1
        return 0

    def sh_remove_dir(self, directory):
        """
        Remove directory recursively
        """
        dangerous_dirs = ["/"]
        for dangerous_dir in dangerous_dirs:
            if dangerous_dir == directory:
                logging.error("Removing directory [%s] is dangerous",
                              directory)
                return -1

        ret = self.sh_run("rm -fr %s" % (directory))
        if ret.cr_exit_status != 0:
            logging.error("failed to remove directory [%s] on host [%s], "
                          "ret = %d, stdout = [%s], stderr = [%s]",
                          directory, self.sh_hostname,
                          ret.cr_exit_status, ret.cr_stdout,
                          ret.cr_stderr)
            return -1
        return 0

    def sh_command_job(self, command, timeout=None, stdout_tee=None,
                       stderr_tee=None, stdin=None):
        """
        Return the command job on a host
        """
        # pylint: disable=too-many-arguments
        full_command = ssh_command(self.sh_hostname, command)
        job = utils.CommandJob(full_command, timeout, stdout_tee, stderr_tee,
                               stdin)
        return job

    def sh_detect_device_fstype(self, device):
        """
        Return the command job on a host
        """
        command = ("blkid -o value -s TYPE %s" % device)
        ret = self.sh_run(command)
        if ret.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = %d, stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          ret.cr_exit_status, ret.cr_stdout,
                          ret.cr_stderr)
            return None
        if ret.cr_stdout == "":
            return None
        lines = ret.cr_stdout.splitlines()
        if len(lines) != 1:
            logging.error("command [%s] on host [%s] has unexpected output "
                          "ret = %d, stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          ret.cr_exit_status, ret.cr_stdout,
                          ret.cr_stderr)
            return None

        return lines[0]

    def sh_mkfs(self, device, fstype):
        """
        Format the device to a given fstype
        """
        command = ("mkfs.%s %s" % (fstype, device))
        ret = self.sh_run(command)
        if ret.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = %d, stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          ret.cr_exit_status, ret.cr_stdout,
                          ret.cr_stderr)
            return -1
        return 0

    def sh_rmdir_if_exist(self, directory):
        """
        Remote an empty directory if it exists
        """
        command = ("test -e %s" % (directory))
        retval = self.sh_run(command)
        if retval.cr_exit_status == 0:
            command = ("rmdir %s" % (directory))
            retval = self.sh_run(command)
            if retval.cr_exit_status:
                logging.error("failed to run command [%s] on host [%s], "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              command,
                              self.sh_hostname,
                              retval.cr_exit_status,
                              retval.cr_stdout,
                              retval.cr_stderr)
                return -1
            return 0
        elif retval.cr_exit_status == 1:
            return 0
        else:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1
        return -1

    def sh_device_umount_all(self, device):
        """
        Check whether the device is mounted
        """
        command = ("cat /proc/mounts | grep \"%s \"" % device)
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0 and retval.cr_exit_status != 1:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1

        for line in retval.cr_stdout.splitlines():
            logging.debug("checking line [%s]", line)
            fields = line.split()
            assert fields[0] == device
            tmp_mount_point = fields[1]
            ret = self.sh_filesystem_umount(tmp_mount_point)
            if ret:
                logging.error("failed to umount [%s]", tmp_mount_point)
                return -1
        return 0

    def sh_filesystem_mounted(self, device, fstype=None, mount_point=None):
        """
        Check whether the device is mounted
        """
        command = ("cat /proc/mounts | grep \"%s \"" % device)
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0 and retval.cr_exit_status != 1:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1

        for line in retval.cr_stdout.splitlines():
            logging.debug("checking line [%s]", line)
            fields = line.split()
            assert fields[0] == device
            tmp_mount_point = fields[1]
            tmp_fstype = fields[2]
            if mount_point is None or tmp_mount_point == mount_point:
                if fstype and tmp_fstype != fstype:
                    logging.error("device [%s] mounted to [%s] with "
                                  "type [%s], not [%s]", device,
                                  tmp_mount_point,
                                  tmp_fstype, fstype)
                    return -1
                else:
                    return 1
        return 0

    def sh_device_mounted(self, device):
        """
        Whether device is mounted
        """
        return self.sh_filesystem_mounted(device)

    def sh_filesystem_mount(self, device, fstype, mount_point,
                            options=None, check_device=True):
        """
        Mount file system
        """
        # pylint: disable=too-many-return-statements,too-many-arguments
        if check_device:
            retval = self.sh_run("test -b %s" % device)
            if retval.cr_exit_status != 0:
                logging.error("device [%s] is not a device", device)
                return -1

        retval = self.sh_run("test -e %s" % mount_point)
        if retval.cr_exit_status != 0:
            retval = self.sh_run("mkdir -p %s" % mount_point)
            if retval.cr_exit_status != 0:
                logging.error("failed to create directory [%s]", mount_point)
                return -1

        retval = self.sh_run("test -d %s" % mount_point)
        if retval.cr_exit_status != 0:
            logging.error("[%s] is not directory", mount_point)
            return -1

        ret = self.sh_filesystem_mounted(device, fstype, mount_point)
        if ret == 1:
            return 0
        elif ret < 0:
            return -1

        option_string = ""
        if options:
            option_string = ("-o %s" % options)
        command = ("mount %s -t %s %s %s" %
                   (option_string, fstype, device, mount_point))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1
        return 0

    def sh_filesystem_umount(self, mount_point):
        """
        Mount file system
        """
        command = ("umount %s" % (mount_point))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1
        return 0

    def sh_filesystem_type(self, path):
        """
        Mount file system
        """
        fstype = None
        command = ("df --output=fstype %s" % (path))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, fstype
        lines = retval.cr_stdout.splitlines()
        if len(lines) != 2:
            logging.error("command [%s] has unexpected output, "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, fstype
        fstype = lines[1]
        return 0, fstype

    def sh_filesystem_df(self, mount_point):
        """
        report file system disk space usage
        """
        total = 0
        used = 0
        available = 0
        command = ("df %s" % (mount_point))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, total, used, available
        lines = retval.cr_stdout.splitlines()
        if len(lines) != 2:
            logging.error("command [%s] has unexpected output on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, total, used, available
        df_pattern = (r"^(?P<device>\S+) +(?P<total>\d+) +(?P<used>\d+) "
                      r"+(?P<available>\d+) +(?P<percentage>\S+) +%s" %
                      (mount_point))
        df_regular = re.compile(df_pattern)
        line = lines[1]
        match = df_regular.match(line)
        if not match:
            logging.error("command [%s] has unexpected output on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, total, used, available
        total = int(match.group("total"))
        used = int(match.group("used"))
        available = int(match.group("available"))

        return 0, total, used, available

    def sh_btrfs_df(self, mount_point):
        """
        report Btrfs file system disk space usage
        only return used bytes, because total bytes is not "accurate" since
        it will grow when keep on using
        """
        used = 0
        command = ("btrfs file df -b %s" % (mount_point))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, used
        lines = retval.cr_stdout.splitlines()
        if len(lines) != 6:
            logging.error("command [%s] has unexpected output, "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, used

        df_pattern = (r"^.+: total=(?P<total>\d+), used=(?P<used>\d+)$")
        df_regular = re.compile(df_pattern)
        # ignore GlobalReserve line
        for line in lines[:5]:
            logging.debug("parsing line [%s]", line)
            match = df_regular.match(line)
            if not match:
                logging.error("command [%s] has unexpected output, "
                              "ret = [%d], stdout = [%s], stderr = [%s]",
                              command,
                              retval.cr_exit_status,
                              retval.cr_stdout,
                              retval.cr_stderr)
                return -1, used
            used += int(match.group("used"))

        return 0, used

    def sh_dumpe2fs(self, device):
        """
        Dump ext4 super information
        Return a direction
        """
        info_dict = {}
        command = ("dumpe2fs -h %s" % (device))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1, info_dict
        lines = retval.cr_stdout.splitlines()
        for line in lines:
            if line == "":
                continue
            name = ""
            pointer = 0
            for character in line:
                if character != ":":
                    name += character
                    pointer += 1
                else:
                    pointer += 1
                    break

            for character in line[pointer:]:
                if character != " ":
                    break
                else:
                    pointer += 1

            value = line[pointer:]
            info_dict[name] = value
            logging.debug("dumpe2fs name: [%s], value: [%s]", name, value)
        return 0, info_dict

    def sh_zfs_get_srvname(self, device):
        """
        Get lustre:svname property of ZFS
        """
        property_name = "lustre:svname"
        command = ("zfs get -H %s %s" % (property_name, device))
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return None

        fields = retval.cr_stdout.split('\t')
        if len(fields) != 4:
            logging.error("invalid output of command [%s] on host [%s], "
                          "stdout = [%s]",
                          command, self.sh_hostname,
                          retval.cr_stdout)
            return None

        if fields[0] != device or fields[1] != property_name:
            logging.error("invalid output of command [%s] on host [%s], "
                          "stdout = [%s]",
                          command, self.sh_hostname,
                          retval.cr_stdout)
            return None

        if fields[2] == "-" or fields[3] == "-":
            logging.error("no property [%s] of device [%s] on host [%s], "
                          "stdout = [%s]",
                          property_name, device, self.sh_hostname,
                          retval.cr_stdout)
            return None

        return fields[2]

    def sh_pkill(self, process_cmd, special_signal=None):
        """
        Kill the all processes that are running command
        """
        signal_string = ""
        if special_signal is not None:
            signal_string = " --signal " + special_signal

        command = ("pkill%s -f -x -c '%s'" % (signal_string, process_cmd))
        logging.debug("start to run command [%s] on host [%s]", command,
                      self.sh_hostname)
        retval = self.sh_run(command)
        if (retval.cr_stderr != "" or
                (retval.cr_exit_status != 0 and retval.cr_exit_status != 1)):
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], "
                          "stdout = [%s], stderr = [%s]",
                          command,
                          self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return -1
        instance_count = retval.cr_stdout.strip()
        logging.debug("killed [%s] instance of multiop [%s]",
                      instance_count, process_cmd)
        return 0

    def sh_md5sum(self, fpath):
        """
        Calculate the md5sum of a file
        """
        command = "md5sum %s | awk '{print $1}'" % fpath
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return None
        return retval.cr_stdout.strip()

    def sh_gunzip_md5sum(self, fpath):
        """
        Use gunzip to decompress the file and then calculate the md5sum
        """
        command = "cat %s | gunzip | md5sum | awk '{print $1}'" % fpath
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return None
        return retval.cr_stdout.strip()

    def sh_unquip_md5sum(self, fpath):
        """
        Use quip to decompress the file and then calculate the md5sum
        """
        command = "cat %s | quip -d -o fastq -c | md5sum | awk '{print $1}'" % fpath
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0 or retval.cr_stderr != "":
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return None
        return retval.cr_stdout.strip()

    def sh_truncate(self, fpath, size):
        """
        Truncate the size of a file
        """
        command = "truncate -s %s %s" % (size, fpath)
        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return retval.cr_exit_status
        return 0

    def sh_fill_random_binary_file(self, fpath, size):
        """
        Generate random binary file with the given size
        """
        block_count = (size + 1048575) / 1048576
        if block_count == 1:
            command = "dd if=/dev/urandom of=%s bs=%s count=1" % (fpath, size)
            written = size
        else:
            command = ("dd if=/dev/urandom of=%s bs=1M count=%s" %
                       (fpath, block_count))
            written = 1048576 * block_count

        retval = self.sh_run(command)
        if retval.cr_exit_status != 0:
            logging.error("failed to run command [%s] on host [%s], "
                          "ret = [%d], stdout = [%s], stderr = [%s]",
                          command, self.sh_hostname,
                          retval.cr_exit_status,
                          retval.cr_stdout,
                          retval.cr_stderr)
            return retval.cr_exit_status
        if written == size:
            return 0
        else:
            return self.sh_truncate(fpath, size)

    def sh_rpm_query(self, rpm_name):
        """
        Find RPM on the host
        """
        command = "rpm -q %s" % rpm_name
        retval = self.sh_run(command)
        if retval.cr_exit_status:
            return -1
        return 0