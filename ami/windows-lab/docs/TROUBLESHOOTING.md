# Troubleshooting Windows Lab AMI Builds

## Browser Shows Certificate Warning

This is unrelated to the AMI. It means the platform nginx certificate is self-signed or expired. Install or renew a trusted certificate for the platform domain and restart nginx.

## Base AMI Cannot Be Launched With 64 GB

Error:

```text
Volume of size 64GB is smaller than snapshot ...
```

Cause: the selected base AMI root snapshot is larger than 64 GB. AWS cannot shrink an EBS snapshot at launch time.

Fix:

- Use the latest official Windows Server 2022 base AMI, or
- Increase `ROOT_VOLUME_SIZE` to at least the base snapshot size.

## Builder Timed Out

Check the builder instance:

```bash
aws ec2 describe-instances --instance-ids i-xxxxxxxx
```

Then RDP into it and inspect:

```text
C:\ProgramData\UNext\golden-ami-build.log
C:\ProgramData\UNext\golden-ami-validation.json
C:\ProgramData\Amazon\EC2-Windows\Launch\Log\
```

Common causes:

- No outbound internet from subnet/NAT/route table.
- Security software blocks Chocolatey downloads.
- Windows Update or first boot initialization is still running.
- MongoDB package download failed.

## Chocolatey Install Fails

Verify outbound HTTPS:

```powershell
Invoke-WebRequest https://community.chocolatey.org/ -UseBasicParsing
Invoke-WebRequest https://nodejs.org/ -UseBasicParsing
Invoke-WebRequest https://www.python.org/ -UseBasicParsing
```

If corporate egress filtering is used, allow package source domains or mirror packages internally.

## MongoDB Service Missing

On the builder VM:

```powershell
Get-Service MongoDB
Get-ChildItem "C:\Program Files\MongoDB\Server\*\bin\mongod.exe"
Get-Content C:\ProgramData\UNext\mongod.cfg
```

The build expects MongoDB to bind locally on `127.0.0.1:27017`.

## React Validation Fails

Check:

```powershell
node --version
npm --version
vite --version
cd C:\LabFiles\React\sample-react-app
npm install
npm run build
```

If npm registry access is blocked, fix egress or configure an internal npm registry.

## VS Code Shortcut Broken

Check the installed path:

```powershell
Get-ChildItem "C:\Program Files\Microsoft VS Code\Code.exe"
Get-Command code
```

The public desktop shortcut is created at:

```text
C:\Users\Public\Desktop\Visual Studio Code.lnk
```

## Google Colab Not Opening

Validate:

```powershell
Invoke-WebRequest https://colab.research.google.com/ -UseBasicParsing
```

If it fails, check DNS, NAT gateway, route tables, proxy settings, and any outbound firewall.

## AMI Builds But Labs Boot Slowly

Recommendations:

- Keep per-lab user data small.
- Do not install software during lab launch.
- Use `c6a.xlarge` or an equivalent current generation type for the requested 4 vCPU / 8 GB RAM.
- Keep the root disk gp3.
- Avoid unnecessary startup apps.
- Enable AWS Windows AMI Fast Launch for large events after validation.

## Safe Cleanup

Successful builds terminate the temporary builder instance automatically. Failed builds are preserved by default for inspection. Set:

```bash
TERMINATE_ON_FAILURE=true
```

only when logs are already collected or when you want failed builders removed automatically.
