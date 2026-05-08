#!/bin/bash
# This script builds the merged single-page index.html
# by combining all 5 page files into one SPA

cd "$(dirname "$0")"

cat > index.html << 'MERGED_EOF'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tsatsral Limo — The operating system for chauffeured ground transport</title>
<meta name="description" content="Tsatsral is the operating system for chauffeured ground transport — reservations, dispatch, drivers, billing. One platform, one schedule, one source of truth.">
MERGED_EOF

echo "Build script created. Now run the node script to generate the full file."
