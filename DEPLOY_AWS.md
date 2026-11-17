# Deploying to AWS: private Amazon ECR + Amazon ECS Express Mode

> **Status: prepared, not performed.** The scripts in `deploy/aws/` were written and reviewed against
> the AWS documentation listed in [section 13](#13-sources) on 2026-11-17. **They have never been
> executed against a live AWS account.** The only checks that were run are: every `.ps1` file parses
> with the Windows PowerShell 5.1 parser with zero errors, every `.json` file parses, a static
> pass confirmed that every function call and parameter name resolves, the configuration loader tests
> in `deploy/aws/tests/Test-DeployConfig.ps1` pass, and `-DryRun` runs of the scripts with a test
> configuration (a dry run prints commands and runs none). No AWS command and no Docker command was
> run. Expect to fix small things on the first real run.
> Steps that rest on a fact the documentation did not settle are marked **VERIFY BEFORE RUNNING**
> and collected in [section 12](#12-facts-that-could-not-be-confirmed).

You run everything yourself, from your own machine, signed in to your own AWS account. Every script
has a `-DryRun` switch (alias `-WhatIf`) that prints the commands and runs none of them. Use it first.

**Running this deployment costs money for as long as it exists**, including while nobody uses it.
Read [section 10](#10-cost-components) before step 4 and run [section 11](#11-cleanup) when you are done.

---

## Contents

**Optional summary configuration added on 2026-11-17:** search still needs no API key. To enable summaries, supply `OPENAI_API_KEY` and a shared random `SUMMARY_SIGNING_KEY` as runtime secrets on every task. Keep the same signing key across replicas and deployments so a result reference can be verified wherever the next request lands. Set `SUMMARY_MODEL` (default `gpt-4o-mini`) as a normal environment variable. The container needs outbound HTTPS access to `api.openai.com` for summary clicks.

The deployment scripts currently pass ordinary environment values only; they do **not** wire secret references into the container configuration. Do not put secret values in `deploy.config.json` or image build arguments. Configure them through the ECS container's `secrets` entries and grant the task execution role access to the selected secrets before enabling summaries. Secret rotation requires new tasks. See the [AWS guide to injecting Secrets Manager values](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html). This summary deployment configuration has not been exercised against AWS. Local configuration and failure behavior are in [README.md](README.md#optional-summaries-of-top-3--top-5-results).

1. [Prerequisites on Windows](#1-prerequisites-on-windows)
2. [What Amazon ECS Express Mode is](#2-what-amazon-ecs-express-mode-is)
3. [Configuration reference](#3-configuration-reference)
4. [Step-by-step deployment](#4-step-by-step-deployment)
5. [Get the HTTPS URL and verify](#5-get-the-https-url-and-verify)
6. [Logs](#6-logs)
7. [Deploy a new image](#7-deploy-a-new-image)
8. [Roll back](#8-roll-back)
9. [Troubleshooting](#9-troubleshooting)
10. [Cost components](#10-cost-components)
11. [Cleanup](#11-cleanup)
12. [Facts that could not be confirmed](#12-facts-that-could-not-be-confirmed)
13. [Sources](#13-sources)

---

## 1. Prerequisites on Windows

### 1.1 Tools

| Tool | Needed for | Notes |
| --- | --- | --- |
| Windows PowerShell 5.1 | all scripts | Already on Windows 11. Use a normal PowerShell window, not the ISE. |
| AWS CLI **v2, 2.36.44 or later** | all scripts | See below for why this version. |
| Docker Desktop, running, in **Linux containers** mode | `02-build-and-push.ps1` | The image is `linux/amd64`. |
| git | optional | Adds the short commit hash to the image tag. |

**Install AWS CLI v2.** The AWS CLI user guide gives three Windows options
([Installing or updating the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)).
The MSI installer for the current user needs no admin rights:

```powershell
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2-User.msi
```

Open a **new** PowerShell window afterwards and check:

```powershell
aws --version
```

The output starts with `aws-cli/2.x.y`. The minimum comes from the AWS CLI v2 changelog:

| Version | What it added |
| --- | --- |
| 2.32.2 | The Express Mode commands (`create-`, `describe-`, `update-`, `delete-express-gateway-service`), `monitor-express-gateway-service` and `--monitor-resources` |
| 2.32.22 | `--mode TEXT-ONLY` for the monitor |
| 2.36.44 | `--cpu-architecture`, which `04-deploy-express-service.ps1` passes |

The command reference used for this review was the one published for AWS CLI 2.40.4.

### 1.2 Sign in

Use IAM Identity Center (single sign-on) or an existing named profile.
**Never paste an access key or secret key into a chat, a ticket, a script or a file in this repository.**
The scripts need no keys in any file: they use whatever the AWS CLI is already signed in to.

```powershell
# One-time setup. The wizard asks for your SSO start URL, SSO Region, account and role,
# then for a default Region and a profile name.
aws configure sso

# Each working day (the session expires):
aws sso login --profile my-profile

# Tell the AWS CLI, and therefore the scripts, which profile to use in this window:
$env:AWS_PROFILE = 'my-profile'
```

Instead of `$env:AWS_PROFILE` you can set `"awsProfile": "my-profile"` in `deploy.config.json`.
Reference: [Configuring IAM Identity Center authentication with the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).

**Check which account and Region you are about to use. Do this every time.**

```powershell
aws sts get-caller-identity      # prints Account, Arn and UserId
aws configure get region         # prints the default Region, or nothing
```

If the second command prints nothing, set a Region:

```powershell
aws configure set region eu-west-1 --profile my-profile
```

The scripts print the account id, Region and signed-in ARN at the start of every run. If you set
`"accountId"` in `deploy.config.json` they stop when you are signed in to a different account.

### 1.3 Permissions your identity needs

The official pages name one requirement explicitly: your identity must be allowed `iam:PassRole` on
the infrastructure role, with `iam:PassedToService` equal to `ecs.amazonaws.com`
([Amazon ECS infrastructure IAM role](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/infrastructure_IAM_role.html)).
Beyond that the scripts call: Amazon ECR (create and delete repository, push, describe, lifecycle policy),
IAM (create, get, tag, delete role; attach, detach, list role policies), Amazon ECS (the Express Mode
operations, service deployments, service revisions, clusters), and read-only calls to EC2, Elastic Load
Balancing, ACM, CloudWatch, CloudWatch Logs (plus retention and delete log group), Application Auto
Scaling, the Resource Groups Tagging API and STS.

**VERIFY BEFORE RUNNING:** the exact IAM action names for the Express Mode operations were not listed on
the pages reviewed. The simplest safe choice is a sandbox account where you have administrator access.

### 1.4 A default VPC

Express Mode places the load balancer and the tasks in the account's **default VPC** and its public
subnets unless you pass your own subnets. The developer guide requires at least two public subnets in
two Availability Zones, each with at least 8 free IP addresses. `00-check-prereqs.ps1` checks this.
If the Region has no default VPC, either choose another Region or create one with
`aws ec2 create-default-vpc` (this creates a VPC, subnets and an internet gateway in your account; see
[Default VPCs](https://docs.aws.amazon.com/vpc/latest/userguide/default-vpc.html)).
The scripts do not create or change any VPC.

### 1.5 Allow the scripts to run

Windows blocks unsigned scripts by default. Allow them for the current window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# From the repository root:
cd deploy\aws
```

---

## 2. What Amazon ECS Express Mode is

Everything in this section comes from the pages in [section 13](#13-sources).

**What it is.** A way to create an Amazon ECS service on AWS Fargate from three inputs: a container
image, a task execution role and an infrastructure role. Amazon ECS then creates and wires the
supporting resources in your account and gives the service an HTTPS URL. It is intended for stateless
web applications and APIs.

**Availability.** Announced on 21 November 2025. The announcement says it is "available now in all AWS
Regions at no additional charge"; the developer guide says it is available "in all AWS Regions where
Amazon ECS and Fargate are supported". Neither page carries a preview label. The announcement does not
use the words "generally available", so this document does not either. There is no charge for Express
Mode itself; you pay for the resources it creates.

**The two IAM roles.**

| Role | Trusted service principal | AWS managed policy |
| --- | --- | --- |
| Task execution role | `ecs-tasks.amazonaws.com` | `arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy` |
| Infrastructure role | `ecs.amazonaws.com` | `arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices` |

The execution role lets the ECS agent pull the image from Amazon ECR and write container logs. The
infrastructure role lets Amazon ECS create, change and delete the load balancer, target groups, security
groups, certificate, auto scaling, alarm and log group; it is used only when the service is created,
updated or deleted. A **task role** is optional and is not created here: the application calls no AWS API.

**What Express Mode creates for you** (developer guide, "Resources created by Amazon ECS Express Mode services"):

- the ECS `default` cluster, if it does not exist, with the Fargate capacity provider
- a task definition (container named `Main`, `awsvpc` networking, Linux, Fargate)
- an ECS service that uses **canary** deployments and auto scaling
- an internet-facing Application Load Balancer with an HTTPS listener on port 443, a host-header
  listener rule and target groups (HTTP to the container, target type `ip`)
- two security groups: one for the load balancer, one for the service
- an AWS Certificate Manager (ACM) certificate
- an Application Auto Scaling scalable target and a target tracking scaling policy
- a CloudWatch metric alarm used to detect a faulty deployment
- a CloudWatch Logs log group for the service
- service-linked roles, if missing: for Amazon ECS, for Elastic Load Balancing and
  `AWSServiceRoleForApplicationAutoScaling_ECSService`

Tasks in public subnets get a public IP address. Up to 25 Express Mode services in the same VPC share
one load balancer.

**Defaults that matter here.** Load balancer health checks: every 30 seconds, 5 second timeout, healthy
after 5 consecutive successes, unhealthy after 2 failures, success code 200. Health check grace period
for a new task: 300 seconds. Container stop timeout: 30 seconds. The image tag is resolved to a digest
when the service revision is created, so all tasks of a revision run the same image.

**The HTTPS URL.** The getting-started guide gives the format `https://<service-name>.ecs.<region>.on.aws/`.
The scripts never build the URL from that pattern. They read it from the API:
`service.activeConfigurations[].ingressPaths[].endpoint` in the output of
`aws ecs describe-express-gateway-service`.

**Deployments and rollback.** Every update is a canary deployment and the strategy cannot be changed
(developer guide, "Updating an Amazon ECS Express Mode service"):

1. new tasks start next to the old ones
2. 5% of traffic moves to the new tasks
3. after a 3 minute bake time, 100% of traffic moves
4. after another 3 minute bake time, the old tasks are stopped

Amazon ECS rolls the deployment back by itself when the alarm fires: the sum of 4xx and 5xx responses
above 1% for 2 data points within 3 minutes. The ECS deployment circuit breaker applies to rolling
deployments only, so it is not what protects an Express Mode deployment; the alarm is.

**CPU architecture.** `X86_64` (the default) or `ARM64`. The image here is `linux/amd64`, so `X86_64`.

**Commands.** All under `aws ecs`:

| Command | Required | Options used by the scripts |
| --- | --- | --- |
| `create-express-gateway-service` | `--infrastructure-role-arn` | `--service-name`, `--cluster`, `--execution-role-arn`, `--primary-container`, `--health-check-path`, `--cpu`, `--memory`, `--cpu-architecture`, `--scaling-target`, `--tags` |
| `describe-express-gateway-service` | `--service-arn` | |
| `update-express-gateway-service` | `--service-arn` | `--primary-container`, `--health-check-path`, `--cpu`, `--memory`, `--scaling-target` |
| `delete-express-gateway-service` | `--service-arn` | |
| `monitor-express-gateway-service` | `--service-arn` | `--mode TEXT-ONLY`, `--resource-view`, `--timeout` (minutes, default 30) |

The JSON shape of `--primary-container`:

```json
{
  "image": "123456789012.dkr.ecr.eu-west-1.amazonaws.com/ticket-similarity@sha256:<digest>",
  "containerPort": 8080,
  "environment": [
    { "name": "PORT", "value": "8080" },
    { "name": "LOG_LEVEL", "value": "INFO" },
    { "name": "MAX_CONCURRENT_SEARCHES", "value": "4" },
    { "name": "SAVE_ENABLED", "value": "false" }
  ]
}
```

Optional members not used here: `awsLogsConfiguration` (`logGroup`, `logStreamPrefix`), `command`,
`secrets`, `repositoryCredentials`. The shape of `--scaling-target`:

```json
{ "minTaskCount": 1, "maxTaskCount": 2, "autoScalingMetric": "AVERAGE_CPU", "autoScalingTargetValue": 60 }
```

`autoScalingMetric` is one of `AVERAGE_CPU`, `AVERAGE_MEMORY`, `REQUEST_COUNT_PER_TARGET`.

---

## 3. Configuration reference

All parameters live in one file. To change any of them:

```powershell
Copy-Item deploy.config.example.json deploy.config.json
notepad deploy.config.json
```

`deploy.config.json` is git-ignored. If it does not exist the scripts use the example file and say so.
The file holds no secrets.

| Field | Default | Meaning |
| --- | --- | --- |
| `awsProfile` | `""` | AWS CLI profile. Empty: whatever the CLI uses (`$env:AWS_PROFILE` or `default`). |
| `region` | `""` | Empty: `AWS_REGION`, then `AWS_DEFAULT_REGION`, then `aws configure get region`. |
| `accountId` | `""` | Empty: taken from `aws sts get-caller-identity`. If set, the scripts stop when it differs from the signed-in account. |
| `projectTag` | `ticket-similarity` | Value of the `Project` tag on created resources. |
| `ecrRepositoryName` | `ticket-similarity` | Private Amazon ECR repository. |
| `ecrScanOnPush` | `true` | Basic vulnerability scan when an image is pushed. |
| `ecrApplyLifecyclePolicy` | `true` | Apply `ecr/lifecycle-policy.json`. |
| `serviceName` | `ticket-similarity` | Express Mode service name. Lowercase letters, digits, hyphens. Cannot be changed later. |
| `clusterName` | `default` | ECS cluster. Express Mode creates `default` when missing. |
| `taskExecutionRoleName` | `ticket-similarity-ecs-task-execution` | IAM role name. |
| `infrastructureRoleName` | `ticket-similarity-ecs-express-infra` | IAM role name. |
| `containerPort` | `8080` | Port the container listens on. Must equal `environment.PORT`. |
| `healthCheckPath` | `/ready` | Load balancer health check path. |
| `cpu` | `"1024"` | CPU units per task (1024 = 1 vCPU). **Confirm with a local measurement.** |
| `memory` | `"2048"` | MiB per task (2048 = 2 GB). **Confirm with a local measurement.** |
| `cpuArchitecture` | `X86_64` | Must match the image. |
| `minTaskCount` / `maxTaskCount` | `1` / `2` | Auto scaling range. One uvicorn worker per task; scale by adding tasks. |
| `autoScalingMetric` | `AVERAGE_CPU` | Scaling metric. |
| `autoScalingTargetValue` | `60` | Target for the metric. |
| `environment` | `PORT`, `LOG_LEVEL`, `MAX_CONCURRENT_SEARCHES`, `SAVE_ENABLED` = `"false"` | Plain environment variables. No secrets belong here. `SAVE_ENABLED` must stay `"false"` unless `TICKET_STORE` is `"postgresql"`; the scripts refuse anything else (see below). |
| `logGroupName` | `""` | Empty: Express Mode names and creates the log group. See section 12 before setting it. |
| `logStreamPrefix` | `ecs` | Used only when `logGroupName` is set. |
| `logRetentionDays` | `14` | Applied after the first deployment. `0` leaves the group at "never expire". |
| `deployByDigest` | `true` | Deploy `repository@sha256:...` rather than `repository:tag`. |
| `deployTimeoutMinutes` | `30` | How long the scripts wait for a deployment. |
| `dockerfilePath` / `dockerBuildContext` | `Dockerfile` / `.` | Relative to the repository root. |
| `dockerPlatform` | `linux/amd64` | Build platform. |
| `dockerDisableProvenance` | `true` | Passes `--provenance=false` so each tag is one manifest. Needs Docker Buildx (Docker Desktop has it). |

**Saving resolved tickets is off by default, and the scripts keep it off with JSON storage.** Without a
`TICKET_STORE` entry the container stores tickets as JSON on the task's own disk, which on Fargate is
temporary and separate for each task. The loader therefore stops with a "Configuration problems" error
when the `environment` block enables saving (`SAVE_ENABLED` missing, `"true"`, or any value the
application does not read as false) while `TICKET_STORE` is not `"postgresql"`, whatever `maxTaskCount`
says. It also stops when `TICKET_STORE` is anything other than `"json"` or `"postgresql"` in lowercase,
because the application would not start. Why, and how to keep saving with PostgreSQL:
[Persistence of saved tickets](#persistence-of-saved-tickets-read-before-enabling-save-resolved-ticket).
To check the loader's rules on your machine (no AWS command is run):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Test-DeployConfig.ps1
```

Valid Fargate sizes for 1 vCPU are 2 GB to 8 GB of memory in 1 GB steps
([Fargate task definition considerations](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html)).

Files the scripts write locally, both git-ignored:

- `deploy/aws/.generated/` holds the JSON documents passed to the AWS CLI as `file://`. They are written
  as UTF-8 without a byte-order mark. A dry run also writes them, so you can read exactly what would be sent.
- `deploy/aws/.state/` holds the last pushed image, the service ARN, a deployment history and the
  resource inventory used by cleanup.

---

## 4. Step-by-step deployment

Run each script with `-DryRun` first, read what it prints, then run it for real. Each script prints
every command before running it and stops at the first failure.

```powershell
# From the repository root:
cd deploy\aws
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### Step 0. Check prerequisites (read-only)

```powershell
.\00-check-prereqs.ps1
```

Checks PowerShell, the AWS CLI version, Docker, git, the configuration file, the Dockerfile, the
signed-in account and Region, and the default VPC. It changes nothing. Fix every `FAIL` before continuing.

### Step 1. Create the private ECR repository

```powershell
.\01-create-ecr.ps1 -DryRun
.\01-create-ecr.ps1
```

Creates the repository with **immutable tags**, scan on push, AES256 encryption and a lifecycle policy
(untagged images expire after 7 days; only the 10 newest images are kept). The script sets no repository
policy, so the repository stays private to your account. Safe to run again.

- With immutable tags, pushing a tag that already exists fails with `ImageTagAlreadyExistsException`.
- The repository-level `scanOnPush` setting is documented as "being deprecated" in favour of a
  registry-level scanning configuration. It is still in the CLI reference. Set `ecrScanOnPush` to
  `false` to leave it out.

### Step 2. Build and push the image

```powershell
.\02-build-and-push.ps1 -DryRun
.\02-build-and-push.ps1
```

Requires the `Dockerfile` at the repository root. The script:

1. builds with `docker build --platform linux/amd64`
2. checks the result really is `linux/amd64` and stops if it is not
3. signs Docker in to ECR. The token from `aws ecr get-login-password` goes to
   `docker login --password-stdin`; it is never printed or stored by the script, and is valid for 12 hours
4. pushes under a unique tag: UTC timestamp plus short commit hash, for example `20261117-101500-a1b2c3d`
5. reads the digest back from ECR and prints it

The last lines show the tag and the `sha256:` digest. The image is about 1.5 GB, so the first push takes
a while. Check the scan result with the command the script prints.

### Step 3. Create the IAM roles

```powershell
.\03-create-iam-roles.ps1 -DryRun
.\03-create-iam-roles.ps1
```

Creates the two roles from section 2 with the trust policies in `deploy/aws/iam/` and attaches the two AWS
managed policies. Idempotent: an existing role is checked, never modified, except that the managed policy
is attached if missing. If a role with the same name exists but trusts a different service, the script
stops; choose another name in the configuration. **No task role is created.**

### Step 4. Create the service

```powershell
.\04-deploy-express-service.ps1 -DryRun
.\04-deploy-express-service.ps1
```

**This is the step that starts costing money.** The script asks you to type `yes`. It then:

1. deploys the image from step 2, by digest
2. stops if the service already exists (use step 7's script for updates)
3. calls `aws ecs create-express-gateway-service`; if Amazon ECS cannot yet assume a role that was
   created moments ago, it waits 60 seconds and retries, up to 3 times (the getting-started guide
   describes this delay)
4. polls until the first deployment succeeds and `GET /ready` answers 200
5. prints the CPU and memory **as Amazon ECS reports them**, so you can compare with what you asked for
6. sets the log retention
7. prints the HTTPS URL

Expect several minutes: the load balancer and certificate are provisioned, the 1.5 GB image is pulled,
the indexes load, and the load balancer needs 5 consecutive successful health checks 30 seconds apart.

To watch in a second window:

```powershell
aws ecs monitor-express-gateway-service --service-arn <service-arn> --mode TEXT-ONLY
```

**VERIFY BEFORE RUNNING:** the unit of `--cpu` and `--memory`. See section 12, item 1.

### Step 5. Verify

```powershell
.\05-verify.ps1
```

Described in the next section.

---

## 5. Get the HTTPS URL and verify

`05-verify.ps1` reads the URL from AWS and checks `/health`, `/ready` and one search. To do the same by hand:

```powershell
# 1. The URL. Use the service ARN printed by step 4.
$service  = aws ecs describe-express-gateway-service --service-arn <service-arn> --output json | ConvertFrom-Json
$endpoint = $service.service.activeConfigurations[0].ingressPaths[0].endpoint
if ($endpoint -notmatch '^https?://') { $endpoint = "https://$endpoint" }
$endpoint

# 2. Liveness: 200 while the process is up.
Invoke-RestMethod -Uri "$endpoint/health"

# 3. Readiness: 200 when at least one search mode is usable, 503 otherwise.
Invoke-RestMethod -Uri "$endpoint/ready"

# 4. A sample search.
$body = @{
    subject     = 'Cannot sign in after changing my password'
    description = 'Since I changed my password this morning the sign-in page shows an error and I cannot reach my account.'
} | ConvertTo-Json
$result = Invoke-RestMethod -Uri "$endpoint/api/search" -Method Post -ContentType 'application/json' -Body $body
$result.status
$result.message
$result.results.Count
```

Expected: `/health` returns `status = alive`; `/ready` returns `status = ready` with a `mode`; the search
returns a `status` of `useful_matches`, `no_strong_match` or `insufficient_input`. Then open `$endpoint/`
in a browser.

If Windows PowerShell 5.1 reports a TLS error, run this once in the window (the scripts already do):

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
```

Test a container on your own machine with the same script: `.\05-verify.ps1 -BaseUrl http://localhost:8080`.

---

## 6. Logs

The application writes JSON lines to stdout, without ticket text. The task definition Express Mode
creates sends stdout to CloudWatch Logs with the `awslogs` driver in non-blocking mode.

- **Log group name.** By default `/aws/ecs/<cluster>/<service-name>-<suffix>`. `04-deploy-express-service.ps1`
  prints the exact name. To find it later:

  ```powershell
  aws logs describe-log-groups --log-group-name-prefix /aws/ecs/default/ticket-similarity
  ```

- **Read or follow the logs.**

  ```powershell
  aws logs tail <log-group-name> --since 30m
  aws logs tail <log-group-name> --follow --format short
  ```

- **Console.** ECS console, Clusters, `default`, the service, **Logs** tab. The **Resources** tab lists
  every resource Express Mode manages, and **Deployments** shows deployment history.

- **Retention.** Express Mode creates the log group with no expiry. Step 4 sets it to `logRetentionDays`
  (14 by default). To change it by hand:

  ```powershell
  aws logs put-retention-policy --log-group-name <log-group-name> --retention-in-days 30
  ```

- **Service events** (task start and stop, health check failures):

  ```powershell
  aws ecs describe-services --cluster default --services ticket-similarity --query "services[0].events[0:10]"
  ```

---

## 7. Deploy a new image

```powershell
.\02-build-and-push.ps1          # builds, pushes under a new unique tag, prints the digest
.\06-update-service.ps1 -DryRun
.\06-update-service.ps1          # deploys that digest
.\05-verify.ps1
```

`06-update-service.ps1` calls `aws ecs update-express-gateway-service` with the new image **and** the
current settings from `deploy.config.json` (port, environment, health check path, CPU, memory, scaling).
The configuration file is therefore the single source of truth: to change the task size or an
environment variable, edit the file and run the same script.

What to expect:

- A canary deployment, as described in section 2. It takes at least the two 3-minute bake times plus task
  start-up, even when everything is healthy.
- During the deployment old and new tasks run side by side, so you pay for both for those minutes.
- **Client errors count.** The rollback alarm sums 4xx and 5xx responses. A burst of 404 or 422 responses
  during the canary window can roll back a healthy deployment. Avoid load tests and broken clients
  while a deployment is running.
- The script records the image that was running before, for rollback.

To deploy a specific image instead of the last pushed one:

```powershell
.\06-update-service.ps1 -ImageUri <repository-uri>@sha256:<digest>
```

---

## 8. Roll back

First find out what state the deployment is in:

```powershell
aws ecs list-service-deployments --service <service-arn> --cluster default
```

Statuses: `PENDING`, `IN_PROGRESS`, `SUCCESSFUL`, `STOPPED`, `STOP_REQUESTED`, `ROLLBACK_REQUESTED`,
`ROLLBACK_IN_PROGRESS`, `ROLLBACK_SUCCESSFUL`, `ROLLBACK_FAILED`.

| Situation | What to do |
| --- | --- |
| Status is `ROLLBACK_IN_PROGRESS` or `ROLLBACK_SUCCESSFUL` | Nothing. Amazon ECS already rolled back because the alarm fired. Read the logs to find out why. |
| The deployment is still `PENDING` or `IN_PROGRESS` and you want it stopped | `.\07-rollback.ps1 -Mode StopInProgress` |
| The deployment finished (`SUCCESSFUL`) but the new version is bad | `.\07-rollback.ps1` |

**`.\07-rollback.ps1`** (mode `PreviousImage`) redeploys the image that ran before, taken from
`deploy/aws/.state/deploy-history.json`, or the one you pass with `-ImageUri`. It is an ordinary canary
deployment of the older image, so it takes the same several minutes. Because tags are immutable and
images are deployed by digest, the older image is byte-for-byte the one that ran before. To list what
is in the repository:

```powershell
aws ecr describe-images --repository-name ticket-similarity --query "sort_by(imageDetails,&imagePushedAt)[].[imagePushedAt,imageTags[0],imageDigest]" --output table
```

**`.\07-rollback.ps1 -Mode StopInProgress`** runs
`aws ecs stop-service-deployment --service-deployment-arn <arn> --stop-type ROLLBACK`, which returns the
service to the previous service revision. **VERIFY BEFORE RUNNING:** see section 12, item 4.

Both modes ask for confirmation (`-Force` skips it) and support `-DryRun`.

Keep in mind that the lifecycle policy keeps only the 10 newest images. An image older than that is gone
and cannot be rolled back to.

---

## 9. Troubleshooting

Start with three views: the service **events**, the **stopped task reason**, and the **application log**.

```powershell
aws ecs describe-services --cluster default --services ticket-similarity --query "services[0].events[0:10]"
aws ecs list-tasks --cluster default --service-name ticket-similarity --desired-status STOPPED
aws ecs describe-tasks --cluster default --tasks <task-arn> --query "tasks[0].[stoppedReason,stopCode,containers[0].reason,containers[0].exitCode]"
aws logs tail <log-group-name> --since 30m
```

Stopped tasks are only listed for a short time after they stop, so look soon after a failure.

### Image pull failures (`CannotPullContainerError`)

| Check | How |
| --- | --- |
| The image exists and the digest is right | `aws ecr describe-images --repository-name ticket-similarity` |
| The execution role has the managed policy | `aws iam list-attached-role-policies --role-name ticket-similarity-ecs-task-execution` |
| Repository and service are in the same Region and account | Compare the image URI with `aws configure get region` |
| The task can reach ECR | In the default VPC the task needs a public IP and a route to an internet gateway. Express Mode enables the public IP for public subnets. A changed route table or network ACL breaks this. |
| Architecture matches | The image must be `linux/amd64` and `cpuArchitecture` `X86_64`. Step 2 checks the image. |
| The image was expired by the lifecycle policy | Only the 10 newest images are kept. |

Reference: [CannotPullContainer task errors](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_cannot_pull_image.html).

### Failing health checks

Symptoms: tasks start, run for a few minutes and are replaced; events say the task "failed ELB health
checks"; the URL returns 503.

1. **Port.** `containerPort` must be the port the process listens on (8080) and `environment.PORT` must
   match. The process must bind `0.0.0.0`, not `127.0.0.1`.
2. **Path.** The health check path is `/ready`. It must return exactly 200; the load balancer treats
   any other code, including the 503 that `/ready` returns while indexes load, as a failure.
3. **Is the application up?** Look for the start-up lines and any traceback in the log.
4. **Target health.** Get the target group ARN from the ECS console (service, Resources tab) and run:

   ```powershell
   aws elbv2 describe-target-health --target-group-arn <target-group-arn>
   ```

   The `Reason` and `Description` fields say whether the check timed out or got the wrong status code.
5. **Test the same image locally**: `docker run --rm -p 8080:8080 <image>` and
   `.\05-verify.ps1 -BaseUrl http://localhost:8080`.

### Slow start-up

Every new Fargate task pulls the whole image; Fargate does not cache images between tasks
([Fargate image pull behavior](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-pull-behavior.html)).
With a 1.5 GB image the pull dominates start-up time, then the model and indexes load, then the load
balancer needs 5 successful checks 30 seconds apart. A new task has a 300 second health check grace
period before failed checks count against it.

- If tasks are replaced before they ever become healthy, start-up is longer than the grace period.
  The grace period and the health check thresholds are not Express Mode parameters. They can be changed
  on the ECS service and on the target group directly (the developer guide allows this and says
  Express Mode does not overwrite such changes unless an Express Mode update conflicts with them):

  ```powershell
  aws ecs update-service --cluster default --service ticket-similarity --health-check-grace-period-seconds 600
  aws elbv2 modify-target-group --target-group-arn <target-group-arn> --healthy-threshold-count 2
  ```

- Smaller image, faster start. More CPU also shortens decompression and model loading.
- Scaling out takes as long as a start-up, so the second task does not help with a sudden spike.

### Out-of-memory kills

Symptoms: the task stops with `OutOfMemoryError: container killed due to memory usage`, or the container
exit code is 137.

1. Confirm with the `describe-tasks` command above.
2. Raise `memory` in `deploy.config.json` (for 1 vCPU: 3072, 4096, up to 8192) and run
   `.\06-update-service.ps1`.
3. If it happens only under load, lower `MAX_CONCURRENT_SEARCHES` so fewer searches run at once.
4. Memory use over time: ECS console, service, **Observability** tab, or the `MemoryUtilization` metric
   in the `AWS/ECS` namespace.

Reference: [OutOfMemoryError errors](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/out-of-memory.html).

### Other

| Symptom | Likely cause |
| --- | --- |
| `Configuration problems ... Saving resolved tickets is enabled` | The `environment` block enables saving with JSON storage, which Fargate cannot keep. Add `"SAVE_ENABLED": "false"`, or see [Persistence of saved tickets](#persistence-of-saved-tickets-read-before-enabling-save-resolved-ticket). |
| `Unable to assume the service linked role` on create | New IAM roles need about a minute to propagate. Step 4 retries. |
| `AccessDenied` mentioning `iam:PassRole` | Your identity may not pass the roles to Amazon ECS. See section 1.3. |
| `aws: error: argument operation: Invalid choice` | The AWS CLI is older than 2.32.2. Update it. |
| `Unknown options: --cpu-architecture` | The AWS CLI is older than 2.36.44. Update it. |
| The CLI rejects a `file://` document | The file has a byte-order mark or is not valid JSON. The scripts write without one; do not re-save generated files with Notepad. |
| `docker login` fails with 400 or "unauthorized" | The token was corrupted on stdin or expired. Run step 2 again in a new PowerShell window. |
| A healthy deployment rolled back | 4xx responses during the canary window. See section 7. |
| The service is stuck `DRAINING` | The infrastructure role was deleted or changed before the delete finished. Restore it. |
| The scripts wait until the timeout although the site works | See section 12, item 3. |

Reference: [Troubleshooting Amazon ECS Express Mode services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-troubleshooting.html).

---

## 10. Cost components

No prices are quoted here because they vary by Region and change. Use the linked pages and the
[AWS Pricing Calculator](https://calculator.aws/). Express Mode itself has no charge.

| Component | What is billed | Pricing page |
| --- | --- | --- |
| AWS Fargate | vCPU time and memory time for every running task, from image pull until the task stops. 20 GiB of ephemeral storage is included. | [Fargate pricing](https://aws.amazon.com/fargate/pricing/) |
| Application Load Balancer | Each hour the load balancer exists, plus Load Balancer Capacity Units for traffic processed. | [Elastic Load Balancing pricing](https://aws.amazon.com/elasticloadbalancing/pricing/) |
| Public IPv4 addresses | Every public IPv4 address, per hour: one per running task, and the load balancer's addresses (one per subnet it is attached to). | [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/), "Public IPv4 Address" tab |
| Amazon ECR | Storage of the images, per GB-month. Data transfer out of ECR. | [Amazon ECR pricing](https://aws.amazon.com/ecr/pricing/) |
| CloudWatch Logs | Log ingestion per GB, and log storage per GB-month. | [Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| CloudWatch alarms and metrics | The deployment alarm Express Mode creates and the alarms behind the target tracking policy. | [Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) |
| Data transfer | Responses sent from the load balancer to the internet. | [Fargate pricing](https://aws.amazon.com/fargate/pricing/), [EC2 data transfer](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer) |
| ACM certificate | See the pricing page for public certificates used with a load balancer. | [AWS Certificate Manager pricing](https://aws.amazon.com/certificate-manager/pricing/) |
| Image scanning | See the ECR pricing page for basic scanning. | [Amazon ECR pricing](https://aws.amazon.com/ecr/pricing/) |

Not billed: IAM roles, service-linked roles, an empty ECS cluster, task definitions, security groups,
the default VPC itself.

### What keeps costing money while nobody uses it

The service does not scale to zero. `minTaskCount` is 1, so with no traffic at all these keep running:

1. **One Fargate task**, 24 hours a day (1 vCPU and 2 GB with the defaults).
2. **The Application Load Balancer**, every hour it exists.
3. **Public IPv4 addresses**: the task's and the load balancer's.
4. **ECR storage** for every image kept (about 1.5 GB each, up to 10 with the lifecycle policy).
5. **Stored logs**, until the retention period removes them.
6. **The CloudWatch alarms.**

During a deployment a second task runs for several minutes. Under load auto scaling can add a second
task (`maxTaskCount` is 2).

To stop the charges, run the cleanup. Set a budget alert first if the account is yours:
[Managing your costs with AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html).

---

## 11. Cleanup

```powershell
.\99-cleanup.ps1 -WhatIf     # prints every command, deletes nothing
.\99-cleanup.ps1             # asks you to type the service name, then deletes
```

Switches: `-KeepEcrRepository`, `-KeepIamRoles`, `-KeepLogs`, `-DeleteEmptyCluster`, `-Force` (no
question), `-TimeoutMinutes`, `-LeftoverWaitMinutes`, `-DeleteRolesAnyway`.

**Order matters.** Amazon ECS uses the infrastructure role to delete the service's resources. The script
deletes the service first, waits until it is gone and its resources have disappeared, and only then
deletes the IAM roles. If anything is left over it keeps the roles and exits with code 2; run it again a
few minutes later. Never delete the infrastructure role by hand while the service still exists.

### Every resource, who removes it, and how to confirm it is gone

Replace names with yours. "Gone" means the command returns an empty list or a "not found" error.

**Created by the scripts**

| # | Resource | Removed by | Confirm |
| --- | --- | --- | --- |
| 1 | ECR repository `ticket-similarity` and every image in it, with its lifecycle policy | `99-cleanup.ps1` (`aws ecr delete-repository --force`) | `aws ecr describe-repositories --repository-names ticket-similarity` returns `RepositoryNotFoundException` |
| 2 | IAM role `ticket-similarity-ecs-task-execution` | `99-cleanup.ps1`, after the service is gone | `aws iam get-role --role-name ticket-similarity-ecs-task-execution` returns `NoSuchEntity` |
| 3 | IAM role `ticket-similarity-ecs-express-infra` | `99-cleanup.ps1`, after the service is gone | `aws iam get-role --role-name ticket-similarity-ecs-express-infra` returns `NoSuchEntity` |
| 4 | Local files `deploy/aws/.state`, `deploy/aws/.generated` | `.generated` by the script; delete `.state` yourself | Look in the folder |
| 5 | Local Docker images | You: `docker image rm <image>` | `docker image ls` |
| 6 | The ECR sign-in stored by Docker | You: `docker logout <account>.dkr.ecr.<region>.amazonaws.com` | It expires after 12 hours anyway |

The script deletes an IAM role only when it carries the tag `ManagedBy=deploy-aws-scripts` and has no
inline policies, so a role that existed before is never removed.

**Created by Express Mode**

| # | Resource | Removed by | Confirm |
| --- | --- | --- | --- |
| 7 | ECS Express Mode service, its tasks, service revisions and deployments | `aws ecs delete-express-gateway-service` (the script) | `aws ecs describe-express-gateway-service --service-arn <arn>` fails or shows `INACTIVE`; `aws ecs list-tasks --cluster default` shows none |
| 8 | Application Load Balancer | The service delete, **unless another Express Mode service shares it** | `aws elbv2 describe-load-balancers` does not list it |
| 9 | HTTPS listener and listener rule | The service delete (the listener stays with a shared load balancer) | `aws elbv2 describe-listeners --load-balancer-arn <arn>` |
| 10 | Target groups | The service delete | `aws elbv2 describe-target-groups` does not list them |
| 11 | Load balancer security group | The service delete | `aws ec2 describe-security-groups --filters Name=tag:AmazonECSManaged,Values=true` |
| 12 | Service security group | The service delete | Same command |
| 13 | ACM certificate | The service delete | `aws acm list-certificates` does not list it |
| 14 | Auto scaling scalable target and scaling policy (and the alarms behind the policy) | The service delete | `aws application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids service/default/ticket-similarity` returns an empty list |
| 15 | CloudWatch deployment alarm | The service delete | `aws cloudwatch describe-alarms --alarm-name-prefix ticket-similarity` |
| 16 | **CloudWatch log group** | **Not removed by the service delete.** The script deletes it (`aws logs delete-log-group`) unless `-KeepLogs` | `aws logs describe-log-groups --log-group-name-prefix /aws/ecs/default/ticket-similarity` returns an empty list |
| 17 | Task definition revisions | The developer guide says the service delete removes the task definition. The script reports any revision still `ACTIVE` and prints the `deregister-task-definition` command. No charge either way. | `aws ecs list-task-definitions --family-prefix ticket-similarity --status ACTIVE` |
| 18 | ECS cluster `default` | Kept, unless you pass `-DeleteEmptyCluster` and it is empty. No charge. | `aws ecs describe-clusters --clusters default` |
| 19 | Service-linked roles `AWSServiceRoleForECS`, `AWSServiceRoleForElasticLoadBalancing`, `AWSServiceRoleForApplicationAutoScaling_ECSService` | **Never removed by the script.** They belong to the whole account, cost nothing and may be used by other workloads. | `aws iam list-roles --path-prefix /aws-service-role/` |
| 20 | Task network interfaces and public IP addresses | Released when the tasks stop | `aws ec2 describe-network-interfaces --filters Name=group-id,Values=<service-security-group-id>` |

**Used, not created:** the default VPC, its subnets and its internet gateway. They stay.

One sweep that catches most leftovers: Express Mode tags what it creates with `AmazonECSManaged=true`.

```powershell
aws resourcegroupstaggingapi get-resources --tag-filters Key=AmazonECSManaged,Values=true --query "ResourceTagMappingList[].ResourceARN"
```

The tag index can keep listing a resource for a while after it was deleted, and it also lists resources
of any other Express Mode service in the Region. Confirm each ARN with the commands above.

**Last check:** open the Billing console the next day and confirm that Fargate, Elastic Load Balancing,
VPC (public IPv4) and ECR show no new usage.

---

## Persistence of saved tickets (read before enabling "Save resolved ticket")

Unless the `environment` block sets `TICKET_STORE`, the container runs in JSON mode (`TICKET_STORE=json`). In that mode, resolutions saved through the form are written to `/app/state` inside the task. **On Fargate that storage is temporary and belongs to one task.** Saved tickets are lost whenever the task is replaced: a new deployment, a rollback, scaling in or a health-check replacement. Whenever two tasks run (auto scaling allows two, and every canary deployment runs old and new tasks side by side), each has its own separate copy, and a save that is retried, for example after a timeout, can reach the other task, which does not know the first save and creates a second ticket.

**The default is a read-only demo.** `deploy.config.example.json` sets `"SAVE_ENABLED": "false"` in the `environment` block. Search works exactly as before; the no-match screen still shows the contact link, but saving is refused with a clear message.

**The scripts enforce it.** The configuration loader (`Get-DeployConfig.ps1`) refuses a configuration that enables saving while tickets are stored as JSON, with any `maxTaskCount`, because even a single task loses its saved tickets on every deployment:

| `environment.SAVE_ENABLED` | `environment.TICKET_STORE` | Result |
| --- | --- | --- |
| `"false"` (also `0`, `no`, `off`, `f`, `n`, in any case) | missing or `"json"` | accepted: read-only demo |
| missing, `"true"` or any other value | missing or `"json"` | **refused** |
| anything | `"postgresql"` | accepted: saving is durable |
| anything | any other value, such as `"PostgreSQL"` or `"JSON"` | **refused**: the application accepts only the two lowercase values |

The loader reads the values the way the application does: a variable name matches in any case (`save_enabled` counts), and a value with surrounding spaces, such as `" false"`, is not read as false. The refusal stops `01` to `04`, `06` and `07-rollback.ps1` (default `PreviousImage` mode) before they run anything, and `00-check-prereqs.ps1` reports it as a failed configuration check. `05-verify.ps1`, `99-cleanup.ps1` and `07-rollback.ps1 -Mode StopInProgress` send no environment to Amazon ECS, so they skip the rule and still work on a service deployed with an older configuration. If your `deploy.config.json` was copied before this rule existed, add `"SAVE_ENABLED": "false"` to its `environment` block.

**To keep saving: PostgreSQL (durable).** Create an Amazon RDS for PostgreSQL instance in the same VPC, run the migrations and the import once from a machine that can reach it (see [POSTGRES.md](POSTGRES.md)), then add `"TICKET_STORE": "postgresql"` to the `environment` block and remove `SAVE_ENABLED` or set it to `"true"`; the loader then accepts the configuration. Pass `DATABASE_URL` through the container definition's `secrets` field from AWS Secrets Manager, never through `environment`. The task execution role then needs `secretsmanager:GetSecretValue` on that one secret, and the RDS security group must allow the task's security group on port 5432. Set `INDEX_REFRESH_INTERVAL_SECONDS=60` when running more than one task.

**Not prepared or tested:** the deployment scripts do not create RDS, a secret, the IAM permission for it, or the security-group rule, and they do not pass `secrets`. Setting `"TICKET_STORE": "postgresql"` therefore satisfies the loader but not the application, which also needs `DATABASE_URL`; add the `secrets` entry to the container definition yourself. Nothing here has been run against AWS.

---

## 12. Facts that could not be confirmed

Each item names the step it affects. Treat these steps as **verify before running**.

| # | Open point | What the documentation says | Affects | How to verify |
| --- | --- | --- | --- | --- |
| 1 | **Unit of `--cpu` and `--memory`.** | The CLI and API references say "CPU units" and "MiB". The developer guide's defaults table says `1024` and `2048`. But the getting-started guide's example passes `--cpu 2 --memory 4` for "2 vCPU and 4 GB". The pages also disagree on the defaults (256/512 versus 1 vCPU/2 GB). | Step 4, step 7 | The scripts send `1024` and `2048`. Step 4 prints what Amazon ECS reports afterwards; `05-verify.ps1` prints it too. If the create call rejects the values, try `"cpu": "1"`, `"memory": "2"` in the configuration (the loader accepts any whole number). |
| 2 | **Default health check path.** | `/ping` in the CLI and API references, `/` in the developer guide. | None | The scripts always send `--health-check-path /ready`. |
| 3 | **Whether Express Mode deployments appear in `aws ecs list-service-deployments`.** | The command is documented for ECS services and lists canary lifecycle stages, and the Express Mode console has a Deployments tab, but no Express Mode page shows the command. | The wait loop in steps 4 and 7; section 8 | If no deployment record appears, step 4 still succeeds once `/ready` answers 200 three times. Step 7 would wait until the timeout: use `-NoWait` and `aws ecs monitor-express-gateway-service` instead. |
| 4 | **`aws ecs stop-service-deployment --stop-type ROLLBACK` on an Express Mode service.** | Documented for ECS service deployments in general, not mentioned on the Express Mode pages. | `07-rollback.ps1 -Mode StopInProgress` | Run with `-DryRun` first. The default mode (redeploy the earlier image) uses only documented Express Mode commands. |
| 5 | **Whether `update-express-gateway-service --primary-container` replaces or merges the container definition.** | Not stated. | Step 7 | The scripts always send the complete definition (image, port, all environment variables), which is correct under either behaviour. |
| 6 | **Whether the log group is deleted with the service.** | "Best practices" says log groups "are retained when the Express Mode service is deleted". "Delete" lists the log group among resources that "may be deleted depending on your selection". The infrastructure managed policy has `logs:CreateLogGroup` but no delete permission. | Section 11, item 16 | The script checks and deletes the log group itself. |
| 7 | **Whether the `default` cluster is deleted with the service.** | "Delete" lists the cluster "if no other services are running", then says a service delete does not remove the default cluster. | Section 11, item 18 | The script reports the cluster; `-DeleteEmptyCluster` removes it when empty. |
| 8 | **Exact URL of the service.** | Format `https://<service-name>.ecs.<region>.on.aws/` with examples that carry a generated suffix. Whether a chosen service name is used unchanged is not stated. | Section 5 | The scripts read the endpoint from the API and never build it. |
| 9 | **A custom `logGroupName`.** | The option exists. Whether Express Mode creates a custom-named group or expects it to exist is not stated. | Configuration | Leave `logGroupName` empty, as the default does. |
| 10 | **Error text for a service that does not exist.** | Not listed for `describe-express-gateway-service`. | Step 4, cleanup | Step 4 treats any failed describe as "does not exist" and shows the CLI message. Cleanup treats only "not found"-like messages as gone and stops on anything else. |
| 11 | **IAM action names your identity needs for Express Mode.** | Only `iam:PassRole` is stated. | Section 1.3 | Use an identity with administrator access in a sandbox account, or check CloudTrail for the denied action. |
| 12 | **Task definition family name.** | The service name "will be used across several resources, including ... Amazon ECS Task Definition". | Section 11, item 17 | The script records the real task definition ARN before deleting. |
| 13 | **Service-linked role name for ECS.** | The Express Mode page writes `ecsServiceRoleForECS`; the ECS service-linked role page names it `AWSServiceRoleForECS`. | Section 11, item 19 | Informational only. |
| 14 | **Whether basic image scanning is charged.** | Not stated on the pages reviewed. | Step 1 | See the ECR pricing page, or set `ecrScanOnPush` to `false`. |
| 15 | **`docker build --provenance=false`** | A Docker Buildx option, not an AWS fact. Not run here. | Step 2 | If your Docker rejects it, set `dockerDisableProvenance` to `false`. |
| 16 | **Sizing.** 1 vCPU and 2 GB is a starting suggestion, not a measurement. | n/a | Step 4 | Measure the container locally (`docker stats`) under a few concurrent searches and adjust `cpu`, `memory` and `MAX_CONCURRENT_SEARCHES`. |

Also not verified, because nothing was executed: every script's run-time behaviour. The helper
techniques the scripts rely on (capturing a native command's stderr under `$ErrorActionPreference = 'Stop'`,
writing JSON without a byte-order mark, sending a secret on stdin without a byte-order mark) were tried
in separate throw-away snippets against `cmd.exe`, not against `aws` or `docker`.

---

## 13. Sources

All retrieved on 2026-11-17.

**Express Mode**

- Overview, pricing, availability: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-overview.html>
- Getting started with the AWS CLI (roles, trust policies, commands, URL format): <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-getting-started.html>
- Resources created and defaults: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-work.html>
- Creating a service: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-create-full.html>
- Updating a service (canary behaviour, alarm): <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-update-full.html>
- Deleting a service: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-delete-task.html>
- Viewing a service: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-view-service.html>
- Best practices (log retention): <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-best-practices.html>
- Updating resources outside Express Mode: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-advanced-customization.html>
- Troubleshooting: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-troubleshooting.html>
- Announcement, 21 November 2025: <https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode/>
- AWS News Blog post: <https://aws.amazon.com/blogs/aws/build-production-ready-applications-without-infrastructure-complexity-using-amazon-ecs-express-mode/>
- API reference, CreateExpressGatewayService: <https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_CreateExpressGatewayService.html>
- CloudFormation resource `AWS::ECS::ExpressGatewayService`: <https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecs-expressgatewayservice.html>

**AWS CLI reference**

- <https://docs.aws.amazon.com/cli/latest/reference/ecs/create-express-gateway-service.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-express-gateway-service.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/update-express-gateway-service.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/delete-express-gateway-service.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/monitor-express-gateway-service.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-service-revisions.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/list-service-deployments.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-service-deployments.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecs/stop-service-deployment.html>
- <https://docs.aws.amazon.com/cli/latest/reference/ecr/create-repository.html>
- <https://docs.aws.amazon.com/cli/latest/reference/logs/tail.html>
- <https://docs.aws.amazon.com/cli/latest/reference/logs/put-retention-policy.html>
- <https://docs.aws.amazon.com/cli/latest/reference/resourcegroupstaggingapi/get-resources.html>
- AWS CLI v2 changelog (versions that added the commands): <https://raw.githubusercontent.com/aws/aws-cli/v2/CHANGELOG.rst>
- Installing the AWS CLI: <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>
- IAM Identity Center sign-in: <https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html>
- Parameters from a file (`file://`): <https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-parameters-file.html>

**IAM**

- Infrastructure role and `iam:PassRole`: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/infrastructure_IAM_role.html>
- Task execution role: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html>
- Managed policy `AmazonECSInfrastructureRoleforExpressGatewayServices`: <https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonECSInfrastructureRoleforExpressGatewayServices.html>
- Managed policy `AmazonECSTaskExecutionRolePolicy`: <https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonECSTaskExecutionRolePolicy.html>
- ECS service-linked role: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using-service-linked-roles-for-clusters.html>

**Amazon ECR**

- Pushing an image, authentication: <https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html>
- Tag immutability: <https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-tag-mutability.html>
- Image scanning: <https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-scanning.html> and <https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-scanning-basic.html>
- Lifecycle policies: <https://docs.aws.amazon.com/AmazonECR/latest/userguide/LifecyclePolicies.html>

**Deployments, Fargate, networking**

- Canary deployments: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/canary-deployment.html>
- Stopping a service deployment: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/stop-service-deployment.html>
- Alarm-based failure detection: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-alarm-failure.html>
- Fargate task sizes and architecture: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html>
- Fargate image pull behaviour: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-pull-behavior.html>
- Fargate ephemeral storage: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-storage.html>
- Stopped task error messages: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/stopped-task-error-codes.html>
- Target group health checks: <https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html>
- Public IPv4 address charges: <https://docs.aws.amazon.com/vpc/latest/userguide/vpc-ip-addressing.html>
- Default VPCs: <https://docs.aws.amazon.com/vpc/latest/userguide/default-vpc.html>

**Pricing** (no prices are quoted in this document)

- <https://aws.amazon.com/fargate/pricing/>
- <https://aws.amazon.com/elasticloadbalancing/pricing/>
- <https://aws.amazon.com/vpc/pricing/>
- <https://aws.amazon.com/ecr/pricing/>
- <https://aws.amazon.com/cloudwatch/pricing/>
- <https://aws.amazon.com/certificate-manager/pricing/>
- <https://calculator.aws/>
