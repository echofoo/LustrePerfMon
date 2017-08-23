#! /bin/sh
# This script to prepare the installation
cd "$(dirname "$0")"
THIS_PATH=`pwd`

#install dependents packages
rpm -ivh python*.rpm
rpm -ivh libyaml*.rpm
rpm -ivh Py*.rpm

TMPDIR="/tmp/estmp_$RANDOM"
mkdir $TMPDIR

for pylib in certifi-2017.7.27.1 pytz-2017.2 six-1.10.0 \
             python-dateutil-2.6.1 urllib3-1.22 idna-2.6 \
             chardet-3.0.4 requests-2.18.4 influxdb-4.1.1
do
  cp pylibs/$pylib.tar.gz $TMPDIR/
  cd $TMPDIR
  tar -xf $pylib.tar.gz
  cd $pylib
  python setup.py install
  cd "$THIS_PATH"
done

#backup old settings
if [ -f /etc/esmon.conf ]
then
  cp -f /etc/esmon.conf /etc/esmon.conf.bak
fi

yum -y remove esmon
pwd
rpm -ivh esmon*.rpm
if [ $? -ne 0 ];then
  echo "ESMON package install failed"
  exit -1
fi

#restore old settings
if [ -f /etc/esmon.conf.bak ]
then
  mv -f /etc/esmon.conf.bak /etc/esmon.conf
fi


echo "******************************************************************************"
echo "*              ESMON package has been installed                              *"
echo "*Please set your servers' information into /etc/esmon.conf                   *"
echo "*And please make sure you can access all these server by ssh with keyfile    *"
echo "*Please close server's firewall or open 3000 port for monitor system         *"
echo "*Then please run esmon_install to continue                                   *"
echo "******************************************************************************"