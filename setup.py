import os
from setuptools import setup

README = open(os.path.join(os.path.dirname(__file__), 'README.rst')).read()

# allow setup.py to be run from any path
os.chdir(os.path.normpath(os.path.join(os.path.abspath(__file__), os.pardir)))

setup(
    name='djangorestframework-drilldown',
    version='0.1.0',
    url='http://github.com/peterh32/django-rest-framework-drilldown',
    license='MIT',
    packages=['rest_framework_drilldown'],
    include_package_data=True,
    description='Django REST API extension enables chained relations, filters, field selectors, limit, offset, etc., via a single view.',
    long_description=README,
    author='Peter Hollingsworth',
    author_email='peter@hollingsworth.net',
    install_requires=['djangorestframework'],
)
