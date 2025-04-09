{ pkgs }: {
  deps = [
    pkgs.ffmpeg
    pkgs.python310
    pkgs.python310Packages.pip
  ];
}