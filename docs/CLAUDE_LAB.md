# Claude Desktop Lab

This feature lets the existing launcher create either a standard Windows lab or a Claude Desktop lab from the same reusable Windows AMI. Existing Windows labs are unchanged because `lab_type` defaults to `windows`.

## Architecture

```text
Admin UI
  -> Launch Wizard: Windows Lab or Claude Lab
  -> FastAPI creates labs and assigns one Claude profile per VM
  -> EC2 launches from the same Windows AMI
  -> User Data creates Windows user, injects Claude profile, registers Claude auto-start
  -> Guacamole opens RDP to the VM
```

## Profile Storage

Recommended S3 layout:

```text
s3://YOUR_CLAUDE_PROFILE_BUCKET/
  claude-profiles/
    siddharthyadav63_ymail_com.zip
```

Each archive should contain one of these layouts:

```text
Claude/
Roaming/Claude/
AppData/Roaming/Claude/
```

Optional local cache data can be included under:

```text
Local/Claude/
AppData/Local/Claude/
```

The main Electron profile target is:

```text
C:\Users\<lab-user>\AppData\Roaming\Claude
```

Claude is launched from one of:

```text
C:\Users\<lab-user>\AppData\Local\Programs\Claude\Claude.exe
C:\Program Files\Claude\Claude.exe
C:\Program Files\AnthropicClaude\Claude.exe
```

## Required `.env`

```env
CLAUDE_PROFILE_BUCKET=your-bucket-name
CLAUDE_PROFILE_PREFIX=claude-profiles/
CLAUDE_PROFILE_IDS=siddharthyadav63_ymail_com
CLAUDE_PROFILE_ARCHIVE_SUFFIX=.zip
CLAUDE_ACCOUNT_EMAIL=siddharthyadav63@ymail.com
CLAUDE_REQUIRE_PROFILE_ARCHIVE=true
CLAUDE_REQUIRE_FAST_LAUNCH=true
CLAUDE_FAST_LAUNCH_MIN_TARGET_COUNT=6
LAB_IAM_INSTANCE_PROFILE=your-windows-lab-instance-profile
```

## IAM

The platform role needs normal EC2 launch permission plus `iam:PassRole` for the lab instance profile.

Claude readiness checks use SSM Run Command, so the platform role also needs:

```json
{
  "Effect": "Allow",
  "Action": [
    "ssm:DescribeInstanceInformation",
    "ssm:SendCommand",
    "ssm:GetCommandInvocation"
  ],
  "Resource": "*"
}
```

The Windows lab instance profile needs SSM managed instance permissions, plus read/write access to the profile prefix. Attach AWS managed policy `AmazonSSMManagedInstanceCore` to the instance profile role, then add:

```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"],
  "Resource": "arn:aws:s3:::YOUR_CLAUDE_PROFILE_BUCKET/claude-profiles/*"
}
```

If the bucket uses SSE-KMS, add `kms:Decrypt` scoped to the KMS key and S3 service.

## Golden AMI Requirements

- Claude Desktop is installed automatically from Anthropic's official Windows MSIX deployment URL if it is missing.
- The boot script does not enable Virtual Machine Platform or force a restart. Keep optional Windows features baked into the AMI if a future Claude workflow needs them.
- AWS CLI v2 installed so User Data can run `aws s3 cp`.
- EC2Launch/User Data enabled.
- No Claude account baked into the AMI.

## Runtime Behavior

For `windows` labs, provisioning follows the existing path.

For `claude` labs:

1. Backend assigns an unused `CLAUDE_PROFILE_IDS` entry.
2. EC2 receives tags:
   - `cloudlab:lab_type=claude`
   - `cloudlab:claude_profile_id=account-XX`
3. User Data downloads `s3://bucket/prefix/account-XX.zip`.
4. It extracts the profile into the lab user's AppData.
5. It creates `CloudLabLaunchClaude`, a scheduled task that starts Claude at user logon.
6. By default, the backend refuses to launch a Claude lab unless the configured archive exists. This keeps Claude labs truly pre-logged-in.
7. If `CLAUDE_REQUIRE_PROFILE_ARCHIVE=false` is intentionally set, the VM starts in manual enrollment mode when the archive is missing:
   - Claude Desktop opens at login.
   - Complete the Claude login and OTP for `siddharthyadav63@ymail.com` inside the VM.
   - Double-click `Save Claude Profile` on the desktop after Claude opens successfully.
   - The VM uploads `siddharthyadav63_ymail_com.zip` back to S3 for future pre-logged-in launches.
8. Logs are written to:
   - `C:\ProgramData\Amazon\EC2-Windows\Launch\Log\cloudlab-bootstrap.log`
   - `C:\ProgramData\CloudLab\ClaudeBootstrap.log`
   - `C:\ProgramData\CloudLab\ClaudeProfileCapture.log`

## Avoiding Session Conflicts

The backend does not assign a Claude profile already used by a scheduled, provisioning, running, stopped, resuming, or budget-blocked Claude lab. Release a profile by terminating the old Claude lab.

For best reliability:

- Keep one Claude account per running VM.
- Prefer On-Demand for Claude labs if session continuity matters.
- Use stop/resume for persistent root disks.
- Replace expired sessions by uploading a fresh `account-XX.zip`.

## Troubleshooting

- `CLAUDE_PROFILE_BUCKET must be configured`: set the bucket in `.env` and redeploy.
- `Only N Claude profile(s) are available`: terminate unused Claude labs or add more profile ids and archives.
- Claude does not open: check `C:\ProgramData\CloudLab\ClaudeBootstrap.log`.
- Profile download fails: verify the instance profile, S3 prefix, KMS decrypt permission, and AWS CLI in the AMI.
- `Claude pre-login profile archive is missing`: upload a logged-in archive to `s3://$CLAUDE_PROFILE_BUCKET/$CLAUDE_PROFILE_PREFIX/siddharthyadav63_ymail_com.zip`.
- `Claude AMI Fast Launch is enabling/disabled` or target pool is too small: wait for `aws ec2 describe-fast-launch-images --image-ids <ami>` to report `enabled` with `SnapshotConfiguration.TargetResourceCount` at least `CLAUDE_FAST_LAUNCH_MIN_TARGET_COUNT`, or lower the setting only if slower first boot is acceptable.
- Claude asks for login: the profile session expired; log in once as `siddharthyadav63@ymail.com`, export a fresh archive, and replace the S3 object.

## Cost and Boot Optimization

- Keep Claude Desktop in the Claude AMI, not installed at boot, for the fastest startup. The current Claude-ready AMI is `ami-048ef4334196ef5c7`, rebuilt from `ami-0f7f1c3f50ab704b5` with the Recycle Bin verified empty.
- Use gp3 root volumes sized only as needed.
- Use schedules and idle stop for On-Demand labs.
- Use Spot only for disposable Claude sessions because Spot labs can be interrupted.
- Scale to 50+ VMs by increasing `CLAUDE_PROFILE_IDS`, storing matching archives, and raising AWS EC2/service quotas.
