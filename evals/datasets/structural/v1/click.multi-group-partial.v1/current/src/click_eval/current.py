"""Commands kept together so Git can prove the path rename.

This stable module description deliberately occupies most of the file.  The
historical and current snapshots therefore remain a Git path rename even
though one public function is renamed and another public function is deleted.
The prose is inert fixture material and is identical in both snapshots.
"""


def publish(target: str) -> None:
    ...
