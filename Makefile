.PHONY: test lint deb clean

test:
	python3 -m unittest

lint:
	python3 -m pyflakes .

# Build dist/webcam-autofocus_<version>_all.deb (needs fakeroot and dpkg-deb).
deb:
	packaging/build-deb.sh $(VERSION)

clean:
	rm -rf dist build *.egg-info
