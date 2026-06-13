#!/bin/sh
# Reproducible minimal KVM-guest kernel for the autokernel fork (boot-time round 3).
# Boots the fork rootfs ~21% faster than the stock generic kernel by dropping the
# device probes that stall boot (SATA link-down 288ms, i8042, md autodetect) and
# building a lean, modules-free image (everything for boot is built-in).
# Needs: libelf-dev, gcc, flex, bison, bc.  ~5 min on 20 cores.
V=7.0.12   # latest stable in the fork's 7.0 series (7.1 is still RC)
wget https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-$V.tar.xz
tar -xf linux-$V.tar.xz && cd linux-$V
make defconfig && make kvm_guest.config
./scripts/config \
  --disable MODULES --disable WERROR --disable SYSTEM_TRUSTED_KEYRING --disable SYSTEM_DATA_VERIFICATION \
  --disable ATA --disable SERIO_I8042 --disable SERIO --disable INPUT_MOUSE --disable KEYBOARD_ATKBD \
  --disable MD --disable BLK_DEV_DM --disable BLK_DEV_LOOP --disable SCSI_LOWLEVEL \
  --disable NETWORK_FILESYSTEMS --disable SUNRPC --disable IPV6 --disable CRYPTO_JITTERENTROPY \
  --disable DRM --disable FB --disable SOUND --disable SND --disable USB_SUPPORT --disable MEDIA_SUPPORT \
  --disable HWMON --disable THERMAL --disable WATCHDOG --disable HID --disable ETHERNET --disable WLAN \
  --disable XFS_FS --disable BTRFS_FS --disable F2FS_FS --disable HPET_TIMER --disable RTC_DRV_CMOS \
  --enable EXT4_FS --enable VIRTIO --enable VIRTIO_PCI --enable VIRTIO_BLK --enable VIRTIO_NET \
  --enable SERIAL_8250 --enable SERIAL_8250_CONSOLE --enable DEVTMPFS --enable DEVTMPFS_MOUNT \
  --enable TMPFS --enable CGROUPS
make olddefconfig
make -j"$(nproc)" bzImage
# boot it:  bootbench.py --kernel arch/x86/boot/bzImage --machine q35 --minimal ...
