# Copyright (C) 2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
""" Setup for composing an image

Adding New Output Types
-----------------------

The new output type must add a kickstart template to ./share/composer/ where the
name of the kickstart (without the trailing .ks) matches the entry in compose_args.

The kickstart should not have any url or repo entries, these will be added at build
time. The %packages section should be the last thing, and while it can contain mandatory
packages required by the output type, it should not have the trailing %end because the
package NEVRAs will be appended to it at build time.

compose_args should have a name matching the kickstart, and it should set the novirt_install
parameters needed to generate the desired output. Other types should be set to False.

"""
import logging
log = logging.getLogger("lorax-composer")

import os
from glob import glob
from math import ceil
import pytoml as toml
import shutil
from uuid import uuid4

from pyanaconda.simpleconfig import SimpleConfigFile

# Use pykickstart to calculate disk image size
from pykickstart.parser import KickstartParser
from pykickstart.version import makeVersion, RHEL7

from pylorax.api.projects import projects_depsolve_with_size, dep_nevra
from pylorax.api.projects import ProjectsError
from pylorax.api.recipes import read_recipe_and_id
from pylorax.imgutils import default_image_name
from pylorax.sysutils import joinpaths


def repo_to_ks(r, url="url"):
    """ Return a kickstart line with the correct args.
    :param r: DNF repository information
    :type r: dnf.Repo
    :param url: "url" or "baseurl" to use for the baseurl parameter
    :type url: str
    :returns: kickstart command arguments for url/repo command
    :rtype: str

    Set url to "baseurl" if it is a repo, leave it as "url" for the installation url.
    """
    cmd = ""
    # url uses --url not --baseurl
    if r.baseurl:
        cmd += '--%s="%s" ' % (url, r.baseurl[0])
    elif r.metalink:
        cmd += '--metalink="%s" ' % r.metalink
    elif r.mirrorlist:
        cmd += '--mirrorlist="%s" ' % r.mirrorlist
    else:
        raise RuntimeError("Repo has no baseurl, metalink, or mirrorlist")

    if r.proxy:
        cmd += '--proxy="%s" ' % r.proxy

    if not r.sslverify:
        cmd += '--noverifyssl'

    return cmd

def start_build(cfg, dnflock, gitlock, branch, recipe_name, compose_type, test_mode=0):
    """ Start the build

    :param cfg: Configuration object
    :type cfg: ComposerConfig
    :param dnflock: Lock and YumBase for depsolving
    :type dnflock: YumLock
    :param recipe: The recipe to build
    :type recipe: str
    :param compose_type: The type of output to create from the recipe
    :type compose_type: str
    :returns: Unique ID for the build that can be used to track its status
    :rtype: str
    """
    share_dir = cfg.get("composer", "share_dir")
    lib_dir = cfg.get("composer", "lib_dir")

    # Make sure compose_type is valid
    if compose_type not in compose_types(share_dir):
        raise RuntimeError("Invalid compose type (%s), must be one of %s" % (compose_type, compose_types(share_dir)))

    with gitlock.lock:
        (commit_id, recipe) = read_recipe_and_id(gitlock.repo, branch, recipe_name)

    # Combine modules and packages and depsolve the list
    # TODO include the version/glob in the depsolving
    module_names = [m["name"] for m in recipe["modules"] or []]
    package_names = [p["name"] for p in recipe["packages"] or []]
    projects = sorted(set(module_names+package_names), key=lambda n: n.lower())
    deps = []
    try:
        with dnflock.lock:
            (installed_size, deps) = projects_depsolve_with_size(dnflock.dbo, projects, with_core=False)
    except ProjectsError as e:
        log.error("start_build depsolve: %s", str(e))
        raise RuntimeError("Problem depsolving %s: %s" % (recipe["name"], str(e)))

    # Read the kickstart template for this type
    ks_template_path = joinpaths(share_dir, "composer", compose_type) + ".ks"
    ks_template = open(ks_template_path, "r").read()

    # How much space will the packages in the default template take?
    ks_version = makeVersion(RHEL7)
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(ks_template+"\n%end\n")
    try:
        with dnflock.lock:
            (template_size, _) = projects_depsolve_with_size(dnflock.dbo, ks.handler.packages.packageList,
                                                             with_core=not ks.handler.packages.nocore)
    except ProjectsError as e:
        log.error("start_build depsolve: %s", str(e))
        raise RuntimeError("Problem depsolving %s: %s" % (recipe["name"], str(e)))
    log.debug("installed_size = %d, template_size=%d", installed_size, template_size)

    # Minimum LMC disk size is 1GiB, and anaconda bumps the estimated size up by 35% (which doesn't always work).
    installed_size = max(1024**3, int((installed_size+template_size) * 1.4))
    log.debug("/ partition size = %d", installed_size)

    # Create the results directory
    build_id = str(uuid4())
    results_dir = joinpaths(lib_dir, "results", build_id)
    os.makedirs(results_dir)

    # Write the recipe commit hash
    commit_path = joinpaths(results_dir, "COMMIT")
    with open(commit_path, "w") as f:
        f.write(commit_id)

    # Write the original recipe
    recipe_path = joinpaths(results_dir, "blueprint.toml")
    with open(recipe_path, "w") as f:
        f.write(recipe.toml())

    # Write the frozen recipe
    frozen_recipe = recipe.freeze(deps)
    recipe_path = joinpaths(results_dir, "frozen.toml")
    with open(recipe_path, "w") as f:
        f.write(frozen_recipe.toml())

    # Write out the dependencies to the results dir
    deps_path = joinpaths(results_dir, "deps.toml")
    with open(deps_path, "w") as f:
        f.write(toml.dumps({"packages":deps}))

    # Save a copy of the original kickstart
    shutil.copy(ks_template_path, results_dir)

    # Create the final kickstart with repos and package list
    ks_path = joinpaths(results_dir, "final-kickstart.ks")
    with open(ks_path, "w") as f:
        with dnflock.lock:
            repos = list(dnflock.dbo.repos.iter_enabled())
        if not repos:
            raise RuntimeError("No enabled repos, canceling build.")

        ks_url = repo_to_ks(repos[0], "url")
        log.debug("url = %s", ks_url)
        f.write('url %s\n' % ks_url)
        for idx, r in enumerate(repos[1:]):
            ks_repo = repo_to_ks(r, "baseurl")
            log.debug("repo composer-%s = %s", idx, ks_repo)
            f.write('repo --name="composer-%s" %s\n' % (idx, ks_repo))

        # Write the root partition and it's size in MB (rounded up)
        f.write('part / --fstype="ext4" --size=%d\n' % ceil(installed_size / 1024**2))

        f.write(ks_template)

        for d in deps:
            f.write(dep_nevra(d)+"\n")

        f.write("%end\n")

    # Setup the config to pass to novirt_install
    log_dir = joinpaths(results_dir, "logs/")
    cfg_args = compose_args(compose_type)

    # Get the title, project, and release version from the host
    if not os.path.exists("/etc/os-release"):
        log.error("/etc/os-release is missing, cannot determine product or release version")
    os_release = SimpleConfigFile("/etc/os-release")
    os_release.read()

    log.debug("os_release = %s", os_release)

    cfg_args["title"] = os_release.get("PRETTY_NAME")
    cfg_args["project"] = os_release.get("NAME")
    cfg_args["releasever"] = os_release.get("VERSION_ID")
    cfg_args["volid"] = ""

    cfg_args.update({
        "compression":      "xz",
        "compress_args":    [],
        "ks":               [ks_path],
        "logfile":          log_dir,
        "timeout":          60,                     # 60 minute timeout
    })
    with open(joinpaths(results_dir, "config.toml"), "w") as f:
        f.write(toml.dumps(cfg_args))

    # Set the initial status
    open(joinpaths(results_dir, "STATUS"), "w").write("WAITING")

    # Set the test mode, if requested
    if test_mode > 0:
        open(joinpaths(results_dir, "TEST"), "w").write("%s" % test_mode)

    log.info("Adding %s (%s %s) to compose queue", build_id, recipe["name"], compose_type)
    os.symlink(results_dir, joinpaths(lib_dir, "queue/new/", build_id))

    return build_id

# Supported output types
def compose_types(share_dir):
    r""" Returns a list of the supported output types

    The output types come from the kickstart names in /usr/share/lorax/composer/\*ks
    """
    return sorted([os.path.basename(ks)[:-3] for ks in glob(joinpaths(share_dir, "composer/*.ks"))])

def compose_args(compose_type):
    """ Returns the settings to pass to novirt_install for the compose type

    :param compose_type: The type of compose to create, from `compose_types()`
    :type compose_type: str

    This will return a dict of options that match the ArgumentParser options for livemedia-creator.
    These are the ones the define the type of output, it's filename, etc.
    Other options will be filled in by `make_compose()`
    """
    _MAP = {"tar":              {"make_iso":                False,
                                 "make_disk":               False,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                True,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              default_image_name("xz", "root.tar"),
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None
                                },
            "live-iso":         {"make_iso":                True,
                                 "make_disk":               False,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "live.iso",
                                 "fs_label":                "Anaconda",     # Live booting may expect this to be 'Anaconda'
                                 "image_only":              False,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None
                                },
            "partitioned-disk": {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "disk.img",
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None
                                },
            "qcow2":            {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_type":              "qcow2",
                                 "qemu_args":               [],
                                 "image_name":              "disk.qcow2",
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None
                                },
            "ext4-filesystem":  {"make_iso":                False,
                                 "make_disk":               False,
                                 "make_fsimage":            True,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "filesystem.img",
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None
                                },
            }
    return _MAP[compose_type]

def move_compose_results(cfg, results_dir):
    """Move the final image to the results_dir and cleanup the unneeded compose files

    :param cfg: Build configuration
    :type cfg: DataHolder
    :param results_dir: Directory to put the results into
    :type results_dir: str
    """
    if cfg["make_tar"]:
        shutil.move(joinpaths(cfg["result_dir"], cfg["image_name"]), results_dir)
    elif cfg["make_iso"]:
        # Output from live iso is always a boot.iso under images/, move and rename it
        shutil.move(joinpaths(cfg["result_dir"], "images/boot.iso"), joinpaths(results_dir, cfg["image_name"]))
    elif cfg["make_disk"] or cfg["make_fsimage"]:
        shutil.move(joinpaths(cfg["result_dir"], cfg["image_name"]), joinpaths(results_dir, cfg["image_name"]))


    # Cleanup the compose directory, but only if it looks like a compose directory
    if os.path.basename(cfg["result_dir"]) == "compose":
        shutil.rmtree(cfg["result_dir"])
    else:
        log.error("Incorrect compose directory, not cleaning up")
