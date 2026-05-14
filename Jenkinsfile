@Library('jenkins-library') _

pipeline {
    agent {
        kubernetes {
            cloud env.TUXGRID_BUILD_CLOUD
            inheritFrom 'platform-builder'
        }
    }

    environment {
        IMAGE = 'harbor.tuxgrid.com/platform/graph-kb'
    }

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    triggers {
        pollSCM('H/5 * * * *')
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Build') {
            steps {
                script { platformBuild() }
            }
        }

        stage('Archive') {
            steps {
                script { platformArchive() }
            }
        }

        stage('Sign') {
            steps {
                script { platformSign() }
            }
        }

        stage('Provenance') {
            steps {
                script { platformBuildProvenance() }
            }
        }

        stage('Deploy') {
            when { branch 'main' }
            steps {
                container('deploy-sec-base') {
                    sh '''
                        skaffold render --build-artifacts=artifacts.json --output=rendered.yaml
                        skaffold apply rendered.yaml
                    '''
                }
            }
        }
    }
}
