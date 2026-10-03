# Push this to GitHub (Manav, once)

The repo is already initialized with `main` and the four lane branches, remote set to https://github.com/Manavpatel06/FLOCK.git.
From a terminal inside this folder:

    git push -u origin main
    git push origin lane-a-world lane-b-node lane-c-radio lane-d-integration

Then on GitHub: Settings → Collaborators → add Manas, Reya, Mansi.
Each teammate: `git clone https://github.com/Manavpatel06/FLOCK.git`, `git checkout lane-<theirs>`, read CONTRIBUTING.md.
Delete this file after pushing (`git rm PUSH_ME.md && git commit -m "docs: remove push note" && git push`).
