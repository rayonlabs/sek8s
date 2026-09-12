"""Entry point for nvidia-gpu-tools CLI.

This ensures nvidia_gpu_tools is imported first (which injects classes into pci.devices),
then calls cli.main.main() to match the behavior of running nvidia_gpu_tools.py as a script.
"""
import nvidia_gpu_tools  # This injects classes into pci.devices
from cli.main import main as cli_main

def main():
    """Entry point function for the nvidia-gpu-tools console script."""
    cli_main()

if __name__ == "__main__":
    main()
