_OCC_REPO_NAME = "onex_change_control"


def _is_occ_repo(repo: str) -> bool:
    return repo.strip().rstrip("/").rsplit("/", maxsplit=1)[-1] == _OCC_REPO_NAME
