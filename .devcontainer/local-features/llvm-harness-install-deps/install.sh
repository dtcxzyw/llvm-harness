#!/usr/bin/env bash

set -eux


if [[ $(id -u) -ne 0 ]]; then
    echo -e 'USER is not root. This script must be run as root. Use sudo, su, or add "USER root" to Dockerfile before running this script.'
    exit 1
fi

if [[ -z ${LLVM_HARNESS_DEPS_DIR} ]]; then
    echo 'LLVM_HARNESS_DEPS_DIR is not set. Set it in Dockerfile to point to a location for saving dependencies that''s going to be installed by this script using for example "ENV LLVM_HARNESS_DEPS_DIR=<location>."'
    exit 1
fi

if [[ -z ${LLVM_HARNESS_INSTALL_SCRIPT_DIR} ]]; then
    echo 'LLVM_HARNESS_INSTALL_SCRIPT_DIR is not set. Set it in Dockerfile to point to a location which is saving the script for installing dependencies using for example "ENV LLVM_HARNESS_INSTALL_SCRIPT_DIR=<location>."'
    exit 1
fi

if [[ ! -d ${LLVM_HARNESS_INSTALL_SCRIPT_DIR} ]]; then
    echo "LLVM_HARNESS_INSTALL_SCRIPT_DIR does not exist or is not a directory which is saving the installation script: ${LLVM_HARNESS_INSTALL_SCRIPT_DIR}"
    exit 1
fi


export DEBIAN_FRONTEND=noninteractive


# Create an admin user for installing dependencies
export LLVM_HARNESS_ADMIN_USERNAME=harness-admin
export LLVM_HARNESS_ADMIN_USER_UID=11011
export LLVM_HARNESS_ADMIN_USER_GID=11011

groupadd -g $LLVM_HARNESS_ADMIN_USER_GID $LLVM_HARNESS_ADMIN_USERNAME
useradd -u $LLVM_HARNESS_ADMIN_USER_UID -g $LLVM_HARNESS_ADMIN_USER_GID -m $LLVM_HARNESS_ADMIN_USERNAME
# Add the user to the sudo group without asking him to input password for his sudo operation
# Note: Avoid using usermod as it always requires the user to input password
echo $LLVM_HARNESS_ADMIN_USERNAME ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/$LLVM_HARNESS_ADMIN_USERNAME
chmod 0440 /etc/sudoers.d/$LLVM_HARNESS_ADMIN_USERNAME

# Install dependencies for llvm-harness by the admin user
mkdir -p $LLVM_HARNESS_DEPS_DIR
chown $LLVM_HARNESS_ADMIN_USERNAME:$LLVM_HARNESS_ADMIN_USERNAME $LLVM_HARNESS_DEPS_DIR
chmod 775 $LLVM_HARNESS_DEPS_DIR # Grant users in our group the same permission as the admin user
sudo -E -u $LLVM_HARNESS_ADMIN_USERNAME HOME=/home/$LLVM_HARNESS_ADMIN_USERNAME /usr/bin/env bash $LLVM_HARNESS_INSTALL_SCRIPT_DIR/install.sh

# Clean up the installation script directory
rm -rf $LLVM_HARNESS_INSTALL_SCRIPT_DIR
