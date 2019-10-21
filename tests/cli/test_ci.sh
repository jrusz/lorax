#!/bin/bash
# Note: execute this file from the project root directory

set -e

. /usr/share/beakerlib/beakerlib.sh

dnf -y install \
  anaconda-tui \
  libgit2-glib \
  libselinux-python3 \
  make \
  pykickstart \
  python3-ansible-runner \
  python3-coverage \
  python3-coveralls \
  python3-flask \
  python3-gevent \
  python3-magic \
  python3-mako \
  python3-mock \
  python3-nose \
  python3-pocketlint \
  python3-pycdlib \
  python3-pylint \
  python3-pyparted \
  python3-toml \
  python3-semantic_version \
  python3-sphinx \
  python3-sphinx-argparse \
  python3-sphinx_rtd_theme \
  python3-rpmfluff \
  python3-librepo \
  python3-pyvmomi \
  beakerlib \
  sudo \
  rsync \
  e2fsprogs \
  xz-lzma-compat \
  pbzip2 \
  squashfs-tools \
  qemu-img \
  which \
  yamllint

cd /lorax

rlJournalStart
    rlPhaseStartTest "check"
    	rlRun "make ci"
    rlPhaseEnd


rlJournalEnd
rlJournalPrintText
