# Cloud Lab Platform

Simple production-ready MVP for browser-accessible temporary Windows labs on AWS.

## Stack

- Next.js, TailwindCSS, TypeScript admin UI
- FastAPI async backend
- PostgreSQL for app data
- Apache Guacamole + guacd for browser RDP
- AWS EC2, Pricing API, S3 for Claude profile archives
- Docker Compose and NGINX

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost` and sign in with the bootstrap admin from `.env`.

## Lab Flow

1. Admin creates a batch with user count, duration, budget, region, instance type, AMI, and idle timeout.
2. Backend calculates hourly Windows EC2 pricing and caps runtime by budget.
3. Backend creates one lab record per user with generated credentials.
4. AWS EC2 launches one Windows instance per user from the golden AMI. New labs try EC2 Spot when enabled, then fall back to On-Demand if configured and Spot capacity is unavailable. Labs are named for easy tracing, for example `UNext-user-001-20260517-dev-lab`, and the EC2 instance `Name` tag uses the same label.
5. Guacamole receives an RDP connection for the VM.
6. The dashboard exposes `/session/{unique_id}` links through the current site origin.
7. Cleanup worker terminates expired, idle, and budget-exhausted labs, removes Guacamole entries, deletes lab-scoped Secrets Manager/SSM credential artifacts, and removes tagged lab volumes. Manual force delete follows the same cleanup path.

## Golden AMI

Use the production Windows developer lab AMI automation in [ami/windows-lab/README.md](ami/windows-lab/README.md).

It builds a reusable Windows Server 2022 AMI with Chrome, VS Code, Node.js/npm, Python/pip, MongoDB, React tooling, Google Colab access, desktop shortcuts, validation checks, cleanup, sysprep, and AWS tags.

Use the AMI ID in the Create Batch form. This keeps provisioning fast because labs clone the prepared image instead of installing software at boot.

Current developer lab AMI in `ap-south-1`: `ami-079ba093634ca5405`.

Recommended learner instance type: `t3a.xlarge` for 4 vCPU and 16 GB RAM when cost is the priority. This AMI removes the EC2Launch date/time wallpaper overlay while preserving the developer tools and desktop shortcuts.

## Claude Desktop labs

The launch UI can now create either standard Windows labs or Claude Desktop labs from the same AMI. Claude labs require a pre-created S3 profile archive for `siddharthyadav63@ymail.com`, install Claude Desktop from Anthropic's official MSIX deployment URL if needed, inject the profile into the Windows user's Electron profile path at boot, and auto-start Claude at user logon. See [docs/CLAUDE_LAB.md](docs/CLAUDE_LAB.md).

## Spot labs

Production Spot defaults:

```env
LAB_SUBNET_IDS=subnet-xxxxxxxx,subnet-yyyyyyyy,subnet-zzzzzzzz
LAB_SPOT_ENABLED=true
LAB_INSTANCE_MARKET=spot
LAB_SPOT_FALLBACK_TO_ON_DEMAND=true
LAB_SPOT_INSTANCE_TYPES=t3a.xlarge,t3.xlarge,c6a.xlarge,c5a.xlarge,c5.xlarge,c6i.xlarge,c7i.xlarge
LAB_SPOT_MAX_PRICE=
LAB_ROOT_VOLUME_SIZE_GB=100
LAB_PROVISION_STAGGER_SECONDS=0
```

Set `LAB_SUBNET_IDS` to subnets in multiple Availability Zones in the lab VPC. EC2 capacity, especially Spot capacity, is AZ-specific, so multi-AZ subnet retry prevents one busy AZ from failing an entire batch. `LAB_SUBNET_ID` is still supported as a single-subnet fallback.

`LAB_PROVISION_STAGGER_SECONDS=0` starts a batch together so learners receive labs at nearly the same time. Raise it only if AWS request-rate throttling appears during very large batches.

Use `LAB_INSTANCE_MARKET=spot` for cost-optimized learner labs. Spot labs use persistent Spot requests with EC2 interruption behavior set to `stop`, so budget exhaustion can stop the VM and budget credit can start it again when Spot capacity is available. With `LAB_INSTANCE_MARKET=auto`, the backend chooses Spot only when the estimated Spot price is lower than On-Demand for the selected Windows instance type; otherwise it launches On-Demand. Do not set `LAB_SPOT_MAX_PRICE` unless you intentionally want to cap bids; leaving it empty lets AWS use the On-Demand price ceiling. The backend records both requested and actual market type, retries the configured Spot instance type list, and falls back to On-Demand when `LAB_SPOT_FALLBACK_TO_ON_DEMAND=true`.

Because this Spot mode can be reclaimed by AWS, interrupted Spot labs are marked `interrupted` and Guacamole access is cleaned up. Existing On-Demand labs keep stop/resume behavior.

`LAB_ROOT_VOLUME_SIZE_GB` controls the gp3 root disk size for new labs. The backend automatically keeps it at least as large as the selected AMI root snapshot, so the current 64 GB AMI stays launchable while smaller future AMIs can lower EBS cost without code changes.

## Security Notes

- Change `JWT_SECRET`, bootstrap admin password, and all database passwords before deployment.
- Do not expose raw RDP publicly.
- Allow TCP 3389 to lab VMs only from the Guacamole/platform security group.
- Generated Windows passwords stay in the application database only while the lab is active; termination scrubs the app record and any matching lab-scoped Secrets Manager or SSM leftovers.
- Use HTTPS through ALB/ACM or NGINX TLS.
- Keep IAM scoped to approved subnets, AMIs, tags, and security groups.
- Serve the platform over HTTPS in production. Guacamole login and tunnel requests carry session credentials; endpoint security tools may block plain HTTP access.

## Deployment

See [docs/PRODUCTION.md](docs/PRODUCTION.md).
