#!/bin/bash
for i in 1 2 3 4 5 6 7 8; do
  echo "=== ATTEMPT $i ===" >> gdown_retry.txt
  python -m gdown --folder "https://drive.google.com/drive/folders/1K8QYHV02BNWMrDPzjDhcRK0v0SXdWHqN" -O test_download --continue >> gdown_retry.txt 2>&1
  rc=$?
  echo "=== ATTEMPT $i exit=$rc ===" >> gdown_retry.txt
  if [ $rc -eq 0 ]; then
    echo "ALL_DONE" >> gdown_retry.txt
    break
  fi
  sleep 5
done
