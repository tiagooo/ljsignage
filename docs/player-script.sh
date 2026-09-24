#!/bin/sh
# ---------------------------------------------------------------------------
# CÓPIA DE REFERÊNCIA do script que corre nos Raspberry Pi das lojas.
# NÃO É PARA ALTERAR NEM PARA INSTALAR — serve apenas para documentar o
# comportamento que a app tem de respeitar (ver docs/SPEC.md §2).
# O VIDEOPATH real pode variar de Pi para Pi.
# ---------------------------------------------------------------------------

# get rid of the cursor so we don't see it when videos are running
setterm -cursor off

# set here the path to the directory containing your videos
VIDEOPATH="/home/tmagalhaes/Videos"

# you can normally leave this alone
SERVICE="omxplayer"

# now for our infinite loop!
while true; do
        if ps ax | grep -v grep | grep $SERVICE > /dev/null
        then
        sleep 1;
else
        for entry in $VIDEOPATH/*
        do
                clear
                omxplayer -z $entry > /dev/null
        done
fi
done
