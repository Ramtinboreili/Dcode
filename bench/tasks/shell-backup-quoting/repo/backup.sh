#!/bin/bash
src=$1
dest=$2
name=$(basename $src)
mkdir -p $dest
cd $(dirname $src)
tar czf $dest/$name.tar.gz $name
echo "backup done: $dest/$name.tar.gz"
