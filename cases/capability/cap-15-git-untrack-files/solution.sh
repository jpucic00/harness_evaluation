git rm -r -q --cached dist server.log config.local.ini
printf 'dist/\n*.log\nconfig.local.ini\n' >> .gitignore
git add .gitignore
git commit -q -m "chore: stop tracking build output, logs and local config"
