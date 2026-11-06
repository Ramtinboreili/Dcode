#!/bin/bash
dir=$1
keep=$2
cd $dir
for f in `ls *.log`; do
    i=$keep
    while [ $i -gt 0 ]; do
        mv $f.$i $f.$((i+1))
        i=$((i-1))
    done
    mv $f $f.1
    rm $f.$keep
    touch $f
    if [ `date +%d` = "01" ]; then
        rm -f $f.*
    fi
done
