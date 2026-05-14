# Build Instructions

There are two options to build the project.

## Option 1: Docker Image

### Step 1. Build the Image

```shell
docker build -t llvm-harness-base:latest -f .devcontainer/Dockerfile .
docker build -t llvm-harness:latest -f Dockerfile --build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g) .
```

Note, it may take ~15 minutes to build the image, depending on your machine.

### Step 2. Clone the Repository

```shell
git clone <path-to-repo>
cd llvm-harness
```

### Step 3. Start the Container

```shell
docker run --rm -it -v $(pwd):/llvm-harness --cap-add=SYS_PTRACE --security-opt seccomp=unconfined llvm-harness:latest
```

Reminder: If you prefer to using local models for example [ollama](https://github.com/ollama/ollama), remember to add `--gpus=all` to the above command for NVIDIA.

### Step 4. Bring Up the Environment

Edit `environments` to fill out all required environment variables and then:

```shell
tmux # We need and only support tmux for now
source ./buildscripts/upenv.sh
```

Note that, each time you open a new terminal that does not inherit the global environments of the terminal executing the above command, you should execute the above command again in your new terminal.

### Step 5. Everything is Done


## Option 2: Build Manually

### Step 1. Clone the Repository

```shell
git clone <path-to-repo>
cd llvm-harness
```

### Step 2. Install Dependencies

You need a directory to save all dependencies, say `./dependencies`

```shell
tmux # We need and only support tmux for now
export LLVM_HARNESS_DEPS_DIR=$PWD/dependencies
./buildscripts/install.sh
```

### Step 3. Bring Up the Environment

Edit `environments` to fill out all required environment variables and then:

```shell
source ./buildscripts/upenv.sh
```

Note that, each time you open a new terminal that does not inherit the global environments of the terminal executing the above command, you should execute the above command again in your new terminal.

### Step 4. Everything is Done
