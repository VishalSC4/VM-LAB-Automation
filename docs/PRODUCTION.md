# Production Deployment Guide

## Architecture

One EC2 host runs Docker Compose for the admin UI, FastAPI API, NGINX, PostgreSQL, Guacamole, guacd, and Guacamole PostgreSQL. Each lab user receives one isolated Windows EC2 instance launched from a golden AMI. RDP is only allowed from the Guacamole host security group.

## AWS Checklist

1. Create an EC2 instance for the platform host.
2. Point `labs.yourdomain.com` in Route53 to the host.
3. Install Docker and the AWS CLI on the host.
4. Create a Windows golden AMI with Chrome, VS Code, AWS CLI, Docker, Python, and developer tools.
5. Create a lab security group allowing TCP 3389 only from the Guacamole/platform host security group.
6. Attach an IAM role to the platform host with least privilege for EC2, Pricing, and Secrets Manager.
7. Copy `.env.example` to `.env` and fill production values.
8. Run `bash scripts/deploy_aws_ec2.sh`.
9. Front the host with HTTPS using an AWS ALB/CloudFront certificate, or add a real TLS server block to NGINX.

## Least Privilege IAM

Allow:

- `ec2:RunInstances`, `ec2:RequestSpotInstances`, `ec2:StartInstances`, `ec2:StopInstances`, `ec2:TerminateInstances`, `ec2:DescribeInstances`, `ec2:DescribeSpotPriceHistory`, `ec2:CreateTags`
- `iam:PassRole` for the configured lab instance profile, if one is used
- `pricing:GetProducts`
- `secretsmanager:CreateSecret`, `secretsmanager:PutSecretValue`, `secretsmanager:DeleteSecret`
- `logs:CreateLogStream`, `logs:PutLogEvents` if shipping logs to CloudWatch

Restrict EC2 actions by VPC, subnet, approved AMI IDs, security group, and tags where possible.

Spot production settings:

```env
LAB_SPOT_ENABLED=true
LAB_INSTANCE_MARKET=spot
LAB_SPOT_FALLBACK_TO_ON_DEMAND=true
LAB_SPOT_INSTANCE_TYPES=c6a.xlarge,c5a.xlarge,c6i.xlarge,c5.xlarge,m6a.xlarge,m5a.xlarge,m5.xlarge
LAB_SPOT_MAX_PRICE=
```

Spot labs use one-time requests and terminate on interruption. Keep On-Demand fallback enabled for learner-facing batches unless the event can tolerate failed launches during Spot capacity shortages.

## HTTPS

For the fastest AWS launch, use an ALB with ACM in front of NGINX and forward HTTPS traffic to port 80 on the platform host. Set `GUACAMOLE_PUBLIC_URL` to your `https://...` public origin only if you need absolute links; otherwise the app stores same-origin `/session/...` links.

The Docker Compose deployment serves the app on HTTPS and redirects HTTP to HTTPS. If `infra/nginx/certs/fullchain.pem` and `infra/nginx/certs/privkey.pem` do not exist, the deploy scripts create a self-signed certificate for the host IP/name. Replace those files with a real certificate for your production domain as soon as DNS is ready.

Do not expose Guacamole or credential APIs over plain HTTP in production. Browser RDP sessions include authentication exchanges, and endpoint protection products may block those requests as password-stealing traffic.
