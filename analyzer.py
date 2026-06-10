"""
Code analysis: extract function/class/import relationships for LightRAG ingestion.
Supports Python (full AST), Groovy/Jenkins (regex + pipeline-aware), and JS/TS/Go/others (regex-based).
"""
import ast
import re
from pathlib import Path

CODE_EXTENSIONS = {
    ".py": "python",
    ".groovy": "groovy",
    ".tf": "terraform",
    ".tfvars": "terraform",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".cpp": "cpp",
    ".c": "c",
    ".cs": "csharp",
    ".sh": "shell",
}

# Files that have no extension but should be treated as code
SPECIAL_FILENAMES: dict[str, str] = {
    "Jenkinsfile": "groovy",
}

# Documentation files: ingested as raw prose (no code-structure analysis)
DOC_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".adoc"}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".mypy_cache", "target", "vendor",
}

MAX_FILE_BYTES = 100_000


def is_code_file(path: Path) -> bool:
    return path.suffix in CODE_EXTENSIONS or path.name in SPECIAL_FILENAMES


def is_doc_file(path: Path) -> bool:
    return path.suffix.lower() in DOC_EXTENSIONS


def _format_doc(path: Path, source: str) -> str:
    """Wrap a documentation file's raw text for ingestion (no code analysis)."""
    return f"File: {path}\nType: documentation\n\n{source}"


def _get_lang(path: Path) -> str:
    if path.name in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[path.name]
    if path.suffix.lower() in DOC_EXTENSIONS:
        return "documentation"
    return CODE_EXTENSIONS.get(path.suffix, "unknown")


def analyze_file(path: Path, source: str) -> str:
    lang = _get_lang(path)
    if lang == "python":
        return _analyze_python(path, source)
    if lang == "groovy":
        return _analyze_groovy(path, source)
    if lang == "terraform":
        return _analyze_terraform(path, source)
    return _analyze_generic(path, source, lang)


def analyze_directory(dir_path: Path) -> list[tuple[Path, str]]:
    """Recursively find and analyze all code and documentation files; also returns a cross-file summary."""
    results: list[tuple[Path, str]] = []

    for path in sorted(dir_path.rglob("*")):
        if not path.is_file():
            continue
        is_code = path.suffix in CODE_EXTENSIONS or path.name in SPECIAL_FILENAMES
        is_doc = is_doc_file(path)
        if not is_code and not is_doc:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            if is_code:
                results.append((path, analyze_file(path, source)))
            else:
                results.append((path, _format_doc(path, source)))
        except Exception as e:
            print(f"  Skipping {path}: {e}", flush=True)

    if results:
        results.append((dir_path / "_codebase_summary.txt", _cross_file_summary(dir_path, results)))

    return results


# ── Python AST analysis ──────────────────────────────────────────────────────

def _analyze_python(path: Path, source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _analyze_generic(path, source, "python")

    out = [f"File: {path}", "Language: Python", ""]

    # Imports
    imports = _collect_python_imports(tree)
    if imports:
        out.append("Imports:")
        out.extend(f"  - {i}" for i in imports)
        out.append("")

    # Top-level classes
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        out.append(f"Class: {node.name} (line {node.lineno})")
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in item.args.args if a.arg != "self"]
                calls = _collect_calls(item)
                out.append(f"  Method: {item.name}({', '.join(args)})")
                if calls:
                    out.append(f"    Calls: {', '.join(sorted(calls))}")
        out.append("")

    # Top-level functions (not inside a class)
    class_bodies = {id(n) for node in ast.walk(tree) if isinstance(node, ast.ClassDef) for n in node.body}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if id(node) in class_bodies:
            continue
        prefix = "Async function" if isinstance(node, ast.AsyncFunctionDef) else "Function"
        args = [a.arg for a in node.args.args]
        ret = ""
        if node.returns:
            try:
                ret = f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass
        out.append(f"{prefix}: {node.name}({', '.join(args)}){ret} (line {node.lineno})")
        calls = _collect_calls(node)
        if calls:
            out.append(f"  Calls: {', '.join(sorted(calls))}")
        out.append("")

    return "\n".join(out)


def _collect_python_imports(tree: ast.AST) -> list[str]:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {mod} import {names}")
    return imports


def _collect_calls(func_node: ast.AST) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(_attr_chain(node.func))
    return calls


def _attr_chain(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_attr_chain(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return "?"


# ── Groovy / Jenkins analysis ────────────────────────────────────────────────
# Strategy: extract what the pipeline DOES (commands, images, credentials,
# artifacts, external systems) rather than how the Groovy code calls itself.
# Jenkins DSL is too dynamic for reliable call-graph analysis.

# CLI tools that tell us which external system a sh command touches
_CLI_SIGNALS: list[tuple[str, str]] = [
    (r"\bterraform\b", "Terraform"),
    (r"\bkubectl\b", "Kubernetes"),
    (r"\bhelm\b", "Helm"),
    (r"\baws\s", "AWS CLI"),
    (r"\bgcloud\b", "GCP CLI"),
    (r"\baz\s", "Azure CLI"),
    (r"\bdocker\s+(?:push|pull|build|run)", "Docker"),
    (r"\bansible(?:-playbook)?\b", "Ansible"),
    (r"\bcurl\b|\bwget\b", "HTTP"),
    (r"\bnpm\b|\byarn\b|\bpnpm\b", "Node.js"),
    (r"\bmvn\b|\bgradle\b", "JVM build"),
    (r"\bpip\b|\bpoetry\b|\buv\b", "Python build"),
    (r"\bsonar-scanner\b", "SonarQube"),
    (r"\bnexus\b|\bnpm publish\b|\bmvn deploy\b", "Artifact registry"),
    (r"\bvault\b", "HashiCorp Vault"),
    (r"\bpacker\b", "Packer"),
]


def _extract_sh_commands(source: str) -> list[str]:
    """Extract shell command strings from sh/bat/powershell calls."""
    cmds: list[str] = []

    # sh 'single line' or sh "double line"
    for m in re.finditer(r'\b(?:sh|bat|powershell)\s+([\'"])(.*?)\1', source, re.DOTALL):
        cmd = m.group(2).strip()
        if cmd and len(cmd) > 3:
            cmds.append(cmd[:200])

    # sh '''triple single''' or sh """triple double"""
    for m in re.finditer(r'\b(?:sh|bat|powershell)\s+(?:\'\'\'|"""|\'\'|"")(.*?)(?:\'\'\'|"""|\'\'|"")', source, re.DOTALL):
        cmd = m.group(1).strip()
        if cmd and len(cmd) > 3:
            cmds.append(cmd[:200])

    # sh(script: '...') or sh(script: "...")
    for m in re.finditer(r'\b(?:sh|bat|powershell)\s*\(\s*script\s*:\s*([\'"])(.*?)\1', source, re.DOTALL):
        cmd = m.group(2).strip()
        if cmd and len(cmd) > 3:
            cmds.append(cmd[:200])

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for c in cmds:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _infer_external_systems(sh_commands: list[str], source: str) -> list[str]:
    """Infer which external systems are touched based on shell commands."""
    combined = "\n".join(sh_commands) + "\n" + source
    found: list[str] = []
    for pattern, label in _CLI_SIGNALS:
        if re.search(pattern, combined) and label not in found:
            found.append(label)
    return found


def _analyze_groovy(path: Path, source: str) -> str:
    out = [f"File: {path}", "Language: Groovy", ""]

    is_jenkinsfile = path.name == "Jenkinsfile" or path.name.startswith("Jenkinsfile.")
    in_vars = any(p == "vars" for p in path.parts)
    in_src  = any(p == "src"  for p in path.parts)

    if is_jenkinsfile:
        out.append("Type: Jenkins Pipeline (Jenkinsfile)")
    elif in_vars:
        out.append(f"Type: Jenkins Shared Library — global variable (vars/{path.name})")
    elif in_src:
        out.append("Type: Jenkins Shared Library — class (src/)")
    else:
        out.append("Type: Groovy script")
    out.append("")

    # Shared library vars/ — surface the API prominently
    if in_vars:
        # Javadoc / block comment immediately before call()
        doc_m = re.search(r'/\*\*(.*?)\*/\s*def\s+call\s*\(', source, re.DOTALL)
        call_m = re.search(r'def\s+call\s*\(([^)]*)\)', source)
        if call_m:
            out.append(f"Global Variable: {path.stem}")
            out.append(f"  Signature: {path.stem}({call_m.group(1).strip()})")
            if doc_m:
                doc = re.sub(r'^\s*\*\s?', '', doc_m.group(1), flags=re.MULTILINE).strip()
                out.append(f"  Description: {doc[:400]}")
            out.append("")

    # @Library annotations
    libs = re.findall(r"@Library\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", source)
    if libs:
        out.append("Jenkins Libraries Loaded:")
        for lib in libs:
            out.append(f"  - @Library('{lib}')")
        out.append("")

    # Pipeline stages
    stages = re.findall(r"\bstage\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", source)
    if stages:
        out.append("Pipeline Stages (in order):")
        for i, stage in enumerate(stages, 1):
            out.append(f"  {i}. {stage}")
        out.append("")

    # Agent
    agent_m = re.search(r"\bagent\s*\{([^}]+)\}", source, re.DOTALL)
    if agent_m:
        agent_text = " ".join(agent_m.group(1).split())[:150]
        out.append(f"Agent: {{ {agent_text} }}")
        out.append("")

    # Parameters
    params = re.findall(
        r"(?:string|booleanParam|choice|password|text|file)\s*\(\s*name\s*:\s*['\"]([^'\"]+)['\"]",
        source,
    )
    if params:
        out.append("Pipeline Parameters:")
        for p in params:
            out.append(f"  - {p}")
        out.append("")

    # Environment variables
    env_block = re.search(r"\benvironment\s*\{([^}]+)\}", source, re.DOTALL)
    if env_block:
        env_vars = re.findall(r"(\w+)\s*=", env_block.group(1))
        if env_vars:
            out.append("Environment Variables Set:")
            for v in env_vars:
                out.append(f"  - {v}")
            out.append("")

    # Shell commands — ground truth of what the pipeline does
    sh_commands = _extract_sh_commands(source)
    if sh_commands:
        out.append("Shell Commands Run:")
        for cmd in sh_commands:
            # Show multiline commands compactly
            compact = " && ".join(line.strip() for line in cmd.splitlines() if line.strip())
            out.append(f"  $ {compact[:200]}")
        out.append("")

    # Docker images
    images: list[str] = []
    for pattern in [
        r"docker\.image\s*\(\s*['\"]([^'\"]+)['\"]",
        r"\bimage\s*:\s*['\"]([^'\"]+:[^'\"]+)['\"]",  # name:tag form avoids false matches
        r"FROM\s+([^\s]+)",
    ]:
        images.extend(re.findall(pattern, source))
    images = list(dict.fromkeys(images))  # deduplicate
    if images:
        out.append("Docker Images Used:")
        for img in images:
            out.append(f"  - {img}")
        out.append("")

    # Credentials
    cred_ids: list[str] = []
    cred_ids.extend(re.findall(r"credentialsId\s*:\s*['\"]([^'\"]+)['\"]", source))
    cred_ids.extend(re.findall(r"sshagent\s*\(\s*\[?\s*['\"]([^'\"]+)['\"]", source))
    cred_ids = list(dict.fromkeys(cred_ids))
    if cred_ids:
        out.append("Credentials Accessed:")
        for cid in cred_ids:
            out.append(f"  - {cid}")
        out.append("")

    # Artifacts produced
    artifacts_out: list[str] = []
    artifacts_out.extend(re.findall(r"archiveArtifacts\s+artifacts\s*:\s*['\"]([^'\"]+)['\"]", source))
    artifacts_out.extend(re.findall(r"archiveArtifacts\s+['\"]([^'\"]+)['\"]", source))
    stash_names = re.findall(r"\bstash\s+(?:name\s*:\s*)?['\"]([^'\"]+)['\"]", source)
    if artifacts_out:
        out.append("Artifacts Produced:")
        for a in artifacts_out:
            out.append(f"  - {a}")
        out.append("")
    if stash_names:
        out.append("Stashes Created:")
        for s in stash_names:
            out.append(f"  - {s}")
        out.append("")

    # Artifacts consumed
    unstash_names = re.findall(r"\bunstash\s+['\"]([^'\"]+)['\"]", source)
    if unstash_names:
        out.append("Stashes Consumed:")
        for s in unstash_names:
            out.append(f"  - {s}")
        out.append("")

    # External systems touched (inferred from sh commands + source)
    systems = _infer_external_systems(sh_commands, source)
    if systems:
        out.append("External Systems Touched:")
        for sys in systems:
            out.append(f"  - {sys}")
        out.append("")

    # Notifications
    notifications: list[str] = []
    if re.search(r"\bslackSend\b", source):
        channels = re.findall(r"channel\s*:\s*['\"]([^'\"]+)['\"]", source)
        notifications.append("Slack" + (f" ({', '.join(channels)})" if channels else ""))
    if re.search(r"\b(?:emailext|mail)\b", source):
        notifications.append("Email")
    if re.search(r"\boffice365ConnectorSend\b", source):
        notifications.append("Teams")
    if notifications:
        out.append(f"Notifications: {', '.join(notifications)}")
        out.append("")

    # Post actions
    post_actions = re.findall(r"\b(always|success|failure|unstable|changed|cleanup)\s*\{", source)
    if post_actions:
        out.append(f"Post Actions: {', '.join(dict.fromkeys(post_actions))}")
        out.append("")

    # Imports (useful for src/ classes)
    imports = re.findall(r"^import\s+([\w.]+)", source, re.MULTILINE)
    if imports:
        out.append("Imports:")
        for imp in imports:
            out.append(f"  - {imp}")
        out.append("")

    out.append("Source preview:")
    out.append(source[:2000])
    return "\n".join(out)


# ── Terraform / HCL analysis ─────────────────────────────────────────────────

def _analyze_terraform(path: Path, source: str) -> str:
    try:
        import hcl2
        data = hcl2.loads(source)
        return _format_terraform(path, data)
    except Exception:
        return _analyze_terraform_regex(path, source)


def _fmt_type(val) -> str:
    return str(val).replace("${", "").replace("}", "")


def _format_terraform(path: Path, data: dict) -> str:
    out = [f"File: {path}", "Language: Terraform (HCL)", ""]

    # terraform {} block — backend + required_providers
    for tf_block in data.get("terraform", []):
        if not isinstance(tf_block, dict):
            continue
        if "backend" in tf_block:
            backend = tf_block["backend"]
            btype = list(backend.keys())[0] if isinstance(backend, dict) else str(backend)
            out.append(f"Backend: {btype}")
            cfg = backend.get(btype, {}) if isinstance(backend, dict) else {}
            for k in ["bucket", "key", "region", "dynamodb_table", "storage_account_name", "container_name"]:
                if isinstance(cfg, dict) and k in cfg:
                    out.append(f"  {k}: {cfg[k]}")
            out.append("")
        if "required_providers" in tf_block:
            rp = tf_block["required_providers"]
            out.append("Required Providers:")
            if isinstance(rp, dict):
                for name, cfg in rp.items():
                    src = cfg.get("source", "") if isinstance(cfg, dict) else ""
                    ver = cfg.get("version", "") if isinstance(cfg, dict) else ""
                    info = ", ".join(x for x in [src, ver] if x)
                    out.append(f"  - {name}" + (f" ({info})" if info else ""))
            out.append("")

    # provider blocks
    for pb in data.get("provider", []):
        if not isinstance(pb, dict):
            continue
        for prov_name, cfg in pb.items():
            out.append(f"Provider: {prov_name}")
            if isinstance(cfg, dict):
                for k in ["region", "profile", "alias", "subscription_id", "project"]:
                    if k in cfg:
                        out.append(f"  {k}: {cfg[k]}")
            out.append("")

    # input variables
    var_blocks = data.get("variable", [])
    if var_blocks:
        out.append("Input Variables:")
        for vb in var_blocks:
            if not isinstance(vb, dict):
                continue
            for name, cfg in vb.items():
                if isinstance(cfg, dict):
                    vtype = _fmt_type(cfg.get("type", "any"))
                    default = cfg.get("default", "<required>")
                    desc = cfg.get("description", "")
                    out.append(f"  - var.{name}: type={vtype}, default={default}")
                    if desc:
                        out.append(f"      {desc}")
                else:
                    out.append(f"  - var.{name}")
        out.append("")

    # outputs
    out_blocks = data.get("output", [])
    if out_blocks:
        out.append("Outputs (exported values):")
        for ob in out_blocks:
            if not isinstance(ob, dict):
                continue
            for name, cfg in ob.items():
                value = str(cfg.get("value", ""))[:80] if isinstance(cfg, dict) else ""
                desc = cfg.get("description", "") if isinstance(cfg, dict) else ""
                out.append(f"  - output.{name}" + (f": {value}" if value else ""))
                if desc:
                    out.append(f"      {desc}")
        out.append("")

    # locals
    for lb in data.get("locals", []):
        if isinstance(lb, dict) and lb:
            out.append("Locals:")
            for name in lb:
                out.append(f"  - local.{name}")
            out.append("")
            break

    # resources
    res_blocks = data.get("resource", [])
    if res_blocks:
        out.append("Resources:")
        for rb in res_blocks:
            if not isinstance(rb, dict):
                continue
            for res_type, instances in rb.items():
                if not isinstance(instances, dict):
                    continue
                for res_name, cfg in instances.items():
                    out.append(f"  - {res_type}.{res_name}")
                    if isinstance(cfg, dict):
                        for attr in ["name", "bucket", "cluster_name", "family", "image", "ami", "instance_type", "vpc_id"]:
                            if attr in cfg:
                                out.append(f"      {attr}: {cfg[attr]}")
                        if "depends_on" in cfg:
                            out.append(f"      depends_on: {cfg['depends_on']}")
        out.append("")

    # data sources
    data_blocks = data.get("data", [])
    if data_blocks:
        out.append("Data Sources:")
        for db in data_blocks:
            if not isinstance(db, dict):
                continue
            for ds_type, instances in db.items():
                if not isinstance(instances, dict):
                    continue
                for ds_name, cfg in instances.items():
                    out.append(f"  - data.{ds_type}.{ds_name}")
                    # Remote state reads get extra prominence — they're cross-repo links
                    if ds_type == "terraform_remote_state" and isinstance(cfg, dict):
                        remote_cfg = cfg.get("config", {})
                        btype = cfg.get("backend", "?")
                        key = remote_cfg.get("key", "?") if isinstance(remote_cfg, dict) else "?"
                        bucket = remote_cfg.get("bucket", "") if isinstance(remote_cfg, dict) else ""
                        out.append(f"      CROSS-REPO STATE READ: backend={btype}, key={key}")
                        if bucket:
                            out.append(f"      bucket: {bucket}")
        out.append("")

    # modules
    mod_blocks = data.get("module", [])
    if mod_blocks:
        out.append("Module Calls:")
        for mb in mod_blocks:
            if not isinstance(mb, dict):
                continue
            for mod_name, cfg in mb.items():
                if not isinstance(cfg, dict):
                    continue
                source = cfg.get("source", "?")
                version = cfg.get("version", "")
                inputs = [k for k in cfg if k not in ("source", "version", "depends_on", "count", "for_each", "providers")]
                out.append(f"  - module.{mod_name}")
                out.append(f"      source: {source}" + (f" @ {version}" if version else ""))
                if inputs:
                    out.append(f"      inputs: {', '.join(inputs)}")
        out.append("")

    return "\n".join(out)


def _analyze_terraform_regex(path: Path, source: str) -> str:
    """Regex fallback when python-hcl2 cannot parse a file."""
    out = [f"File: {path}", "Language: Terraform (HCL) [regex fallback]", ""]
    patterns = {
        "Resource": r'^resource\s+"([^"]+)"\s+"([^"]+)"',
        "Data source": r'^data\s+"([^"]+)"\s+"([^"]+)"',
        "Module": r'^module\s+"([^"]+)"',
        "Variable": r'^variable\s+"([^"]+)"',
        "Output": r'^output\s+"([^"]+)"',
        "Provider": r'^provider\s+"([^"]+)"',
    }
    for label, pattern in patterns.items():
        found = []
        for i, line in enumerate(source.splitlines(), 1):
            m = re.match(pattern, line.strip())
            if m:
                name = ".".join(g for g in m.groups() if g)
                found.append(f"  - {name} (line {i})")
        if found:
            out.append(f"{label}s:")
            out.extend(found)
            out.append("")
    sources = re.findall(r'source\s*=\s*"([^"]+)"', source)
    if sources:
        out.append("Sources referenced:")
        out.extend(f"  - {s}" for s in sources)
        out.append("")
    return "\n".join(out)


# ── Terraform cross-repo graph ────────────────────────────────────────────────

def _extract_terraform_meta(dir_path: Path) -> dict:
    """Scan all .tf files in a directory and extract cross-repo linkage metadata."""
    try:
        import hcl2
    except ImportError:
        return {"dir": dir_path, "module_calls": [], "remote_state_reads": [], "outputs": [], "variables": [], "resources": [], "providers": []}

    meta: dict = {
        "dir": dir_path,
        "module_calls": [],       # {name, source, version, inputs, file}
        "remote_state_reads": [], # {name, backend, key, bucket, file}
        "outputs": [],            # {name, description}
        "variables": [],          # {name, description}
        "resources": [],          # "type.name"
        "providers": [],          # "name"
    }

    for tf_file in sorted(dir_path.rglob("*.tf")):
        if any(p in SKIP_DIRS for p in tf_file.parts):
            continue
        try:
            data = hcl2.loads(tf_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue

        for mb in data.get("module", []):
            if not isinstance(mb, dict):
                continue
            for name, cfg in mb.items():
                if not isinstance(cfg, dict):
                    continue
                meta["module_calls"].append({
                    "name": name,
                    "source": cfg.get("source", ""),
                    "version": cfg.get("version", ""),
                    "inputs": [k for k in cfg if k not in ("source", "version", "depends_on", "count", "for_each", "providers")],
                    "file": tf_file.name,
                })

        for db in data.get("data", []):
            if not isinstance(db, dict):
                continue
            for ds_type, instances in db.items():
                if ds_type != "terraform_remote_state" or not isinstance(instances, dict):
                    continue
                for name, cfg in instances.items():
                    if not isinstance(cfg, dict):
                        continue
                    remote_cfg = cfg.get("config", {})
                    meta["remote_state_reads"].append({
                        "name": name,
                        "backend": cfg.get("backend", ""),
                        "key": remote_cfg.get("key", "") if isinstance(remote_cfg, dict) else "",
                        "bucket": remote_cfg.get("bucket", "") if isinstance(remote_cfg, dict) else "",
                        "file": tf_file.name,
                    })

        for ob in data.get("output", []):
            if not isinstance(ob, dict):
                continue
            for name, cfg in ob.items():
                meta["outputs"].append({
                    "name": name,
                    "description": cfg.get("description", "") if isinstance(cfg, dict) else "",
                })

        for vb in data.get("variable", []):
            if not isinstance(vb, dict):
                continue
            for name, cfg in vb.items():
                meta["variables"].append({
                    "name": name,
                    "description": cfg.get("description", "") if isinstance(cfg, dict) else "",
                })

        for rb in data.get("resource", []):
            if not isinstance(rb, dict):
                continue
            for res_type, instances in rb.items():
                if isinstance(instances, dict):
                    for res_name in instances:
                        entry = f"{res_type}.{res_name}"
                        if entry not in meta["resources"]:
                            meta["resources"].append(entry)

        for pb in data.get("provider", []):
            if not isinstance(pb, dict):
                continue
            for prov_name in pb:
                if prov_name not in meta["providers"]:
                    meta["providers"].append(prov_name)

    return meta


def build_terraform_cross_repo_graph(repo_dirs: list[Path]) -> str:
    """
    Scan multiple Terraform repo directories and produce a cross-repo dependency graph.
    Surfaces: module source wiring, remote state reads, output→variable name matches,
    and a full resource inventory across repos.
    """
    repos = [_extract_terraform_meta(d) for d in repo_dirs]

    out = ["Terraform Cross-Repo Dependency Graph", "=" * 42, ""]
    out.append(f"Repos analyzed: {len(repos)}")
    for r in repos:
        out.append(
            f"  - {r['dir'].name}: {len(r['resources'])} resources, "
            f"{len(r['outputs'])} outputs, {len(r['variables'])} variables, "
            f"{len(r['providers'])} providers"
        )
    out.append("")

    # Module call graph
    out.append("Module Call Graph:")
    has_modules = any(r["module_calls"] for r in repos)
    if has_modules:
        for repo in repos:
            if not repo["module_calls"]:
                continue
            out.append(f"\n  {repo['dir'].name}:")
            for call in repo["module_calls"]:
                source = call["source"]
                version = f" @ {call['version']}" if call["version"] else ""
                out.append(f"    → module.{call['name']}")
                out.append(f"        source: {source}{version} (in {call['file']})")
                if call["inputs"]:
                    out.append(f"        inputs: {', '.join(call['inputs'])}")
                # Resolve local path references to other repos in this analysis
                if source.startswith("./") or source.startswith("../"):
                    resolved = (repo["dir"] / source).resolve()
                    for other in repos:
                        if other["dir"].resolve() == resolved:
                            out.append(f"        ↳ resolves to repo: {other['dir'].name}")
    else:
        out.append("  (none detected)")
    out.append("")

    # Remote state reads
    out.append("Remote State Dependencies (cross-repo state reads):")
    has_remote = any(r["remote_state_reads"] for r in repos)
    if has_remote:
        for repo in repos:
            if not repo["remote_state_reads"]:
                continue
            out.append(f"\n  {repo['dir'].name} reads remote state:")
            for rs in repo["remote_state_reads"]:
                out.append(f"    → data.terraform_remote_state.{rs['name']} (backend: {rs['backend']})")
                if rs["key"]:
                    out.append(f"        key:    {rs['key']}")
                if rs["bucket"]:
                    out.append(f"        bucket: {rs['bucket']}")
    else:
        out.append("  (none detected)")
    out.append("")

    # Output → variable name matches (potential cross-repo wiring)
    out.append("Output → Variable Name Matches (potential cross-repo wiring):")
    matches = []
    for provider in repos:
        for output in provider["outputs"]:
            for consumer in repos:
                if consumer["dir"] == provider["dir"]:
                    continue
                for mv in consumer["variables"]:
                    if mv["name"] == output["name"]:
                        matches.append(
                            f"  {provider['dir'].name}.output.{output['name']}"
                            f"  →  {consumer['dir'].name}.var.{mv['name']}"
                        )
    out.extend(matches if matches else ["  (no matching names found)"])
    out.append("")

    # Full resource inventory
    out.append("Resource Inventory (all repos):")
    for repo in repos:
        if not repo["resources"]:
            continue
        out.append(f"\n  {repo['dir'].name}:")
        for res in sorted(repo["resources"]):
            out.append(f"    - {res}")
    out.append("")

    out.append("This cross-repo map is now in the knowledge graph.")
    out.append("You can query: what manages X resource, how modules chain together,")
    out.append("which repos share state, what a repo exports and who consumes it.")
    return "\n".join(out)


# ── Generic regex-based analysis ─────────────────────────────────────────────

_PATTERNS: dict[str, dict[str, str]] = {
    "javascript": {
        "Function": r"(?:^|\s)(?:async\s+)?function\s+(\w+)\s*\(|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\()",
        "Class": r"\bclass\s+(\w+)",
        "Import": r"from\s+['\"](.+?)['\"]|require\(['\"](.+?)['\"]\)",
    },
    "typescript": {
        "Function": r"(?:^|\s)(?:async\s+)?function\s+(\w+)\s*\(|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\()",
        "Class": r"\bclass\s+(\w+)",
        "Interface": r"\binterface\s+(\w+)",
        "Type": r"\btype\s+(\w+)\s*=",
        "Import": r"from\s+['\"](.+?)['\"]",
    },
    "go": {
        "Function": r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(",
        "Struct": r"^type\s+(\w+)\s+struct",
        "Interface": r"^type\s+(\w+)\s+interface",
        "Import": r'^\s+"([^"]+)"',
    },
    "rust": {
        "Function": r"(?:pub\s+)?fn\s+(\w+)\s*[\(<]",
        "Struct": r"(?:pub\s+)?struct\s+(\w+)",
        "Trait": r"(?:pub\s+)?trait\s+(\w+)",
        "Impl": r"impl(?:<[^>]+>)?\s+(\w+)",
    },
    "java": {
        "Class": r"(?:public|private|protected)?\s*(?:abstract\s+)?class\s+(\w+)",
        "Interface": r"(?:public\s+)?interface\s+(\w+)",
        "Method": r"(?:public|private|protected)\s+\w[\w<>\[\]]*\s+(\w+)\s*\(",
        "Import": r"^import\s+([\w.]+);",
    },
}

_DEFAULT_PATTERNS = {
    "Function": r"(?:def|function|func|fn|sub)\s+(\w+)\s*[\(\{]",
    "Class": r"(?:class|struct|type)\s+(\w+)",
    "Import": r"(?:import|require|use|include)\s+['\"]?(\S+?)['\"]?[\s;]",
}


def _analyze_generic(path: Path, source: str, lang: str) -> str:
    out = [f"File: {path}", f"Language: {lang}", ""]
    patterns = _PATTERNS.get(lang, _DEFAULT_PATTERNS)
    source_lines = source.splitlines()

    for label, pattern in patterns.items():
        found = []
        for i, line in enumerate(source_lines, 1):
            m = re.search(pattern, line, re.MULTILINE)
            if m:
                name = next((g for g in m.groups() if g), None)
                if name:
                    found.append(f"  - {name} (line {i})")
        if found:
            out.append(f"{label}s:")
            out.extend(found)
            out.append("")

    # Include raw source (truncated) for semantic understanding
    out.append("Source preview:")
    out.append(source[:2000])
    return "\n".join(out)


# ── Cross-file summary ────────────────────────────────────────────────────────

def _cross_file_summary(root: Path, results: list[tuple[Path, str]]) -> str:
    lang_counts: dict[str, int] = {}
    for path, _ in results:
        lang = _get_lang(path)
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    lang_str = ", ".join(f"{lang} ({n})" for lang, n in sorted(lang_counts.items()))

    out = [
        f"Codebase: {root.name}",
        f"Total files analyzed: {len(results)}",
        f"Languages: {lang_str}",
        "",
        "Files:",
    ]
    for path, _ in results:
        out.append(f"  - {path.relative_to(root)}")

    out += [
        "",
        "This codebase has been fully ingested into the knowledge graph.",
        "You can query function definitions, call relationships, imports,",
        "class hierarchies, and data flows across all files.",
    ]
    return "\n".join(out)
