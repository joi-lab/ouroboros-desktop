import os
import platform
import subprocess
import shutil

def generate_world_profile(output_path: str):
    """Generates a WORLD.md file containing the system profile and hardware details."""
    
    # Get basic OS info
    os_name = platform.system()
    os_release = platform.release()
    arch = platform.machine()
    
    # Get memory
    mem_total = "Unknown"
    try:
        if os_name == "Darwin":
            mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
            mem_total = f"{mem_bytes / (1024**3):.1f} GB"
        elif os_name == "Linux":
            mem_total = subprocess.check_output(["awk", "/MemTotal/ {print $2/1024/1024 \" GB\"}", "/proc/meminfo"]).strip().decode()
    except Exception:
        pass
        
    # Get CPU
    cpu_info = platform.processor()
    try:
        if os_name == "Darwin":
            cpu_info = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).strip().decode()
    except Exception:
        pass
        
    # User and paths
    user = os.environ.get("USER", "unknown")
    cwd = os.getcwd()
    
    # Check for CLI tools
    tools = []
    for tool in ["git", "python3", "pip", "npm", "node", "claude"]:
        if shutil.which(tool):
            tools.append(tool)
            
    content = f"""# WORLD.md — Environment Profile

This is where I currently exist. It defines my hardware, OS, and local constraints.

## System
- **OS**: {os_name} {os_release} ({arch})
- **CPU**: {cpu_info}
- **RAM**: {mem_total}
- **User**: {user}
- **Current Directory**: {cwd}

## Available Tools
The following binaries are available in my PATH:
`{', '.join(tools)}`

## File System Rules
I live inside `~/Documents/Ouroboros/`. 
- `repo/` contains my codebase.
- `data/` contains my memory, state, and logs.
I should generally confine my writes to these directories, though I have read access to the rest of the filesystem if needed for exploration.

*(Generated automatically on first boot)*
"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    generate_world_profile("WORLD.md")
