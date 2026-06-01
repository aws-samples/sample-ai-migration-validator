# Publishing this sample to GitHub (`aws-samples`)

Step-by-step for moving this repository into the [`aws-samples`](https://github.com/aws-samples) GitHub organization.

## 1. Pre-flight

- [ ] You have an [AWS Open Source CLA](https://github.com/aws/aws-cla) on file.
- [ ] You have asked the AWS open-source team to create a new repo under `aws-samples` (typical name: `sql-to-postgres-migration-validator`).
- [ ] You have `gh` (the GitHub CLI) installed and authenticated.

## 2. Initialize the local repo

```bash
git init
git add .
git commit -m "feat: initial agentic SQL → PostgreSQL migration validator"
```

## 3. Create the remote and push

If the empty repo already exists at `aws-samples/sql-to-postgres-migration-validator`:

```bash
git branch -M main
git remote add origin git@github.com:aws-samples/sql-to-postgres-migration-validator.git
git push -u origin main
```

If you need to create it via `gh`:

```bash
gh repo create aws-samples/sql-to-postgres-migration-validator \
    --public \
    --description "Agentic validator for SQL Server → PostgreSQL migrations (AWS Sample)" \
    --homepage "https://aws.amazon.com/database/" \
    --source . \
    --remote origin \
    --push
```

## 4. Configure the repo

Recommended settings under **Settings → General**:

- Default branch: `main`
- Allow squash merges only.
- Enable **automatic deletion of branches** on merge.
- Disable **Wiki** and **Projects** unless you actually use them.

Under **Settings → Branches → Branch protection rules** add a rule for `main`:

- Require pull request reviews (1 approval).
- Require status checks: `CI / lint`, `CI / test`, `CI / security`.
- Require linear history.
- Require signed commits (recommended for sample repos).

Under **Settings → Secrets and variables → Actions** add (only if you wire e2e tests):

- `AWS_ROLE_ARN` — an OIDC role with read access to a *test* Bedrock model id.

## 5. Topics & metadata

Add topics on the repo home page:

```
aws  aws-sample  bedrock  strands  mcp  postgresql  sqlserver  migration  validator  agentic
```

Set the description and link it to the relevant AWS Database Blog post or DMS documentation.

## 6. Pre-publish checklist

- [ ] No real hostnames, account ids, or keys in any file (run `git grep -E 'AKIA|aws_access_key|password\s*='`).
- [ ] Every committed file ends with a trailing newline.
- [ ] `pyproject.toml`, `LICENSE`, `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md` all present.
- [ ] CI (`.github/workflows/ci.yml`) is green on `main`.
- [ ] `bandit` and `pip-audit` report no high-severity issues.

## 7. Tag a release

```bash
git tag -a v0.1.0 -m "Initial release"
git push --tags
gh release create v0.1.0 --generate-notes
```
