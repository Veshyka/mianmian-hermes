## Description: <br>
Manage torrents with qBittorrent by listing, adding, pausing, resuming, deleting, checking status, viewing speed and stats, and handling qBittorrent torrent management requests. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[jmagar](https://clawhub.ai/user/jmagar) <br>

### License/Terms of Use: <br>
MIT <br>


## Use Case: <br>
Developers, operators, and qBittorrent users use this skill to control a configured qBittorrent WebUI from an agent workflow. It supports torrent listing, metadata inspection, adding torrents, pausing or resuming transfers, deletion, rechecks, categories, tags, transfer statistics, and speed-limit changes. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: The skill can delete torrents and downloaded files through qBittorrent WebUI commands. <br>
Mitigation: Require explicit confirmation before delete --files and before broad operations such as all-target control. <br>
Risk: The skill stores a reusable WebUI session cookie in a predictable temporary file. <br>
Mitigation: Protect the runtime environment, restrict file permissions, and clear or override the cookie path when needed. <br>
Risk: Poorly protected WebUI credentials or an exposed qBittorrent WebUI could allow unwanted torrent control. <br>
Mitigation: Use a non-default password, keep the WebUI bound to localhost or protected by HTTPS, and protect the credentials file. <br>


## Reference(s): <br>
- [ClawHub skill page](https://clawhub.ai/jmagar/skills/qbittorrent) <br>
- [Artifact README](artifact/README.md) <br>
- [Artifact SKILL.md](artifact/SKILL.md) <br>
- [qBittorrent WebUI helper script](artifact/scripts/qbit-api.sh) <br>


## Skill Output: <br>
**Output Type(s):** [Text, Markdown, Shell commands, Configuration, API calls, JSON] <br>
**Output Format:** [Markdown guidance with shell commands and JSON responses from qBittorrent WebUI API calls] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Requires configured qBittorrent WebUI credentials or equivalent environment variables.] <br>

## Skill Version(s): <br>
1.0.0 (source: frontmatter and server release metadata) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
