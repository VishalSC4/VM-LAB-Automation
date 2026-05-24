# Windows Developer Lab AMI

This package builds a reusable Windows Server 2022 AMI for UNext-style browser-accessible learner labs.

The image includes:

- Google Chrome
- Visual Studio Code
- Python 3 and pip
- Node.js and npm
- React tooling through Vite and Create React App
- MongoDB service and MongoDB shell
- Google Colab desktop shortcut and connectivity validation
- Public desktop shortcuts for common learner workflows

## Folder Structure

```text
ami/windows-lab/
  README.md
  docs/
    TROUBLESHOOTING.md
  scripts/
    build-windows-lab-ami.sh
    provision-windows-lab.ps1
    validate-windows-lab.ps1
```

## Recommended Builder

Use `c6a.xlarge` for the AMI builder and learner labs when the client asks for 8 GB RAM and 4 vCPU.

Recommended learner VM:

- Instance type: `c6a.xlarge`
- Root disk: `64` GB `gp3`
- IOPS: `3000`
- Throughput: `125`
- OS: Windows Server 2022

## AMI Naming Convention

Use a name that captures ownership, OS, software stack, and build date:

```text
unext-win2022-devlab-vscode-node-python-mongo-react-chrome-YYYYMMDDHHMMSS
```

Example:

```text
unext-win2022-devlab-vscode-node-python-mongo-react-chrome-20260518173000
```

## Build

From a Linux host or the platform EC2 server with AWS CLI configured:

```bash
cd /opt/cloud-lab-platform
REGION=ap-south-1 \
SUBNET_ID=subnet-xxxxxxxx \
SECURITY_GROUP_ID=sg-xxxxxxxx \
INSTANCE_TYPE=c6a.xlarge \
ROOT_VOLUME_SIZE=64 \
bash ami/windows-lab/scripts/build-windows-lab-ami.sh
```

The build script will:

1. Find the latest official Windows Server 2022 base AMI if `BASE_AMI_ID` is not provided.
2. Launch a temporary Windows builder instance.
3. Install all required software silently.
4. Configure PATH and machine-wide developer tooling.
5. Configure MongoDB as an automatic Windows service.
6. Create a validated React sample project.
7. Create desktop shortcuts.
8. Validate the full environment.
9. Sysprep and shut down the builder.
10. Create and tag the AMI and root snapshot.
11. Terminate the temporary builder on success.

## Optional Overrides

```bash
BASE_AMI_ID=ami-xxxxxxxx          # use a specific Windows base AMI
AMI_NAME=custom-name              # custom AMI name
BUILDER_TIMEOUT_MINUTES=120       # max provisioning wait
TERMINATE_ON_FAILURE=true         # terminate failed builder instead of preserving it
SKIP_SYSPREP=true                 # stop without sysprep for debugging only
```

Do not use an old base AMI with a root snapshot larger than the requested root disk size. AWS cannot shrink EBS snapshots. The builder checks this and fails early.

## Deployment

After the build succeeds, use the generated AMI ID in the platform Create Batch form or set it as the frontend default if you want it pre-filled.

The output file is:

```text
ami/windows-lab/build-output.env
```

Example:

```text
AMI_ID=ami-0123456789abcdef0
AMI_NAME=unext-win2022-devlab-vscode-node-python-mongo-react-chrome-20260518173000
REGION=ap-south-1
```

## Validation

The AMI is created only after these checks pass:

- Chrome executable and CLI work
- VS Code executable and CLI work
- Python works
- pip works
- Node.js works
- npm works
- Vite works
- Create React App works
- React sample app builds
- MongoDB service is running
- `mongosh` can ping MongoDB
- Internet connectivity works
- Google Colab is reachable
- Required desktop shortcuts exist
- Eclipse and Jupyter are not present

Validation output is written inside the builder VM:

```text
C:\ProgramData\UNext\golden-ami-validation.json
C:\ProgramData\UNext\GOLDEN_AMI_READY.txt
C:\ProgramData\UNext\GOLDEN_AMI_FAILED.txt
```

## Security

- No learner credentials are baked into the AMI.
- No app secrets are stored in the AMI.
- MongoDB binds to `127.0.0.1` by default.
- RDP is enabled for the platform bootstrap flow, but the lab security group should allow TCP 3389 only from the Guacamole/platform security group.
- Generated learner passwords should continue to be stored outside the image, for example in AWS Secrets Manager.

## Fast Launch Recommendations

- Keep software installation inside the AMI, not in per-lab user data.
- Keep per-lab user data limited to user/password/bootstrap tagging.
- Use gp3 root volumes.
- Avoid oversized images.
- Use one AMI per course/software stack.
- Keep the learner instance type aligned with the client requirement: `c6a.xlarge` for 4 vCPU and 8 GB RAM.
- Consider AWS Windows AMI Fast Launch for very high concurrency after the AMI is stable.
