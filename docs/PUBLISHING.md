# Publish the clean source

Publish the contents of the prepared **UltramicStudio source folder**. Its parent
preparation folder may contain private audit files and validation environments.
Use the source ZIP only as a transport copy: extract it, then publish its contents.

## GitHub Desktop

1. Install [GitHub Desktop](https://desktop.github.com/) and sign in.
2. Choose **File → Add local repository** and select the clean source folder.
   If it has no Git repository, use Desktop's option to create a repository there.
3. Keep the supplied README, LICENSE, and .gitignore; do not replace them with templates.
4. Review the complete changed-file list, including hidden `.github`,
   `.gitignore`, and `.gitattributes`. There should be no recordings, environments,
   SDKs, executables, diagnostics, or private preparation reports.
5. Commit the reviewed files and choose **Publish repository**.
   Name it `UltramicStudio` and clear **Keep this code private** for the requested
   public repository. Read the file list once more before publishing.
6. Open the repository on GitHub and check that README images and documentation
   links work. Check the **Actions** tab for the recorder workflow result.

GitHub's [existing-project guide](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)
also describes Git and CLI upload methods. A browser upload must include hidden
configuration files and preserve subdirectories.

## Account and repository settings

- Configure your [GitHub-provided noreply commit email](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address)
  before the first commit if you want to keep your personal email private.
- Enable [push protection](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/prevent-future-leaks/enable-push-protection).
  GitHub scans public repositories for supported secrets; also review personal
  identifiers and recording data, which credential scanners do not generally detect.
- Confirm that you have authority to publish all original source under the included
  MIT license. Dependencies and vendor components retain separate terms.
- Store recordings and research data outside the repository, with independent backups.

## Future changes

Review every staged change and use the source manifest when exporting another ZIP.
Ignore rules prevent accidental additions of matching untracked files; they do not
remove anything already committed. If a real credential is ever exposed, revoke or
rotate it and follow GitHub's sensitive-data removal guidance. Deleting it in a later
commit alone does not remove it from repository history.

This publication contains source and synthetic illustrations. Distributing Windows
binaries or research datasets is a separate release decision.
