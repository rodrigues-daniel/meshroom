import logging
import os
from threading import Thread

from PySide2.QtCore import QObject, Slot, Property, Signal

from meshroom import multiview
from meshroom.common.qt import QObjectListModel
from meshroom.core import graph
from meshroom.ui.graph import UIGraph


class LiveSfmManager(QObject):
    """
    Manage a live SfM reconstruction by creating augmentation steps in the graph over time,
    based on images progressively added to a watched folder.

    File watching is based on regular polling and not filesystem events to work on network mounts.
    """
    def __init__(self, reconstruction):
        super(LiveSfmManager, self).__init__(reconstruction)
        self.reconstruction = reconstruction
        self._folder = ''
        self.timerId = -1
        self.minImagesPerStep = 4
        self.watchTimerInterval = 1000
        self.allImages = []
        self.cameraInit = None
        self.sfm = None
        self._running = False

    def reset(self):
        self.stop(False)
        self.sfm = None
        self.cameraInit = None

    def setRunning(self, value):
        if self._running == value:
            return
        if self._running:
            self.killTimer(self.timerId)
        else:
            self.timerId = self.startTimer(self.watchTimerInterval)
        self._running = value
        self.runningChanged.emit()

    @Slot(str, int)
    def start(self, folder, minImagesPerStep):
        """
        Start live SfM augmentation.

        Args:
            folder (str): the folder to watch in which images are added over time
            minImagesPerStep (int): minimum number of images in an augmentation step
        """
        # print('[LiveSfmManager] Watching {} for images'.format(folder))
        if not os.path.isdir(folder):
            raise RuntimeError("Invalid folder provided: {}".format(folder))
        self._folder = folder
        self.folderChanged.emit()
        self.cameraInit = self.sfm = None
        self.allImages = self.imagesInReconstruction()
        self.minImagesPerStep = minImagesPerStep
        self.setRunning(True)
        self.update()  # trigger initial update

    @Slot()
    def stop(self, requestCompute=True):
        """ Stop the live SfM reconstruction.

        Request the computation of the last augmentation step if any.
        """
        self.setRunning(False)
        if requestCompute:
            self.computeStep()

    def timerEvent(self, evt):
        self.update()

    def update(self):
        """
        Look for new images in the watched folder and create SfM augmentation step (or modify existing one)
        to include those images to the reconstruction.
        """
        # Get all new images in the watched folder
        filesInFolder = [os.path.join(self._folder, f) for f in os.listdir(self._folder)]
        imagesInFolder = [f for f in filesInFolder if Reconstruction.isImageFile(f)]
        newImages = set(imagesInFolder).difference(self.allImages)
        for imagePath in newImages:
            # print('[LiveSfmManager] New image file : {}'.format(imagePath))
            if not self.cameraInit:
                # Start graph modification: until 'computeAugmentation' is called, every commands
                # used will be part of this macro
                self.reconstruction.beginModification("SfM Augmentation")
                # Add SfM augmentation step in the graph
                self.cameraInit, self.sfm = self.reconstruction.addSfmAugmentation()
                self.stepCreated.emit()
            self.addImageToStep(imagePath)

        # If we have enough images and the graph is not being computed, compute augmentation step
        if len(self.imagesInStep()) >= self.minImagesPerStep and not self.reconstruction.computing:
            self.computeStep()

    def addImageToStep(self, path):
        """ Add an image to the current augmentation step. """
        self.reconstruction.appendAttribute(self.cameraInit.viewpoints, {'path': path})
        self.allImages.append(path)

    def imagePathsInCameraInit(self, node):
        """ Get images in the given CameraInit node. """
        assert node.nodeType == 'CameraInit'
        return [vp.path.value for vp in node.viewpoints]

    def imagesInStep(self):
        """ Get images in the current augmentation step. """
        return self.imagePathsInCameraInit(self.cameraInit) if self.cameraInit else []

    def imagesInReconstruction(self):
        """ Get all images in the reconstruction. """
        return [vp.path.value for node in self.reconstruction.cameraInits for vp in node.viewpoints]

    @Slot()
    def computeStep(self):
        """ Freeze the current augmentation step and request its computation.
        A new step will be created once another image is added to the watched folder during 'update'.
        """
        if not self.cameraInit:
            return

        # print('[LiveSfmManager] Compute SfM augmentation')
        # Build intrinsics in the main thread
        self.reconstruction.buildIntrinsics(self.cameraInit, [])
        self.cameraInit = None
        sfm = self.sfm
        self.sfm = None
        # Stop graph modification and start sfm computation
        self.reconstruction.endModification()
        self.reconstruction.execute(sfm)

    stepCreated = Signal()
    runningChanged = Signal()
    running = Property(bool, lambda self: self._running, notify=runningChanged)
    folderChanged = Signal()
    folder = Property(str, lambda self: self._folder, notify=folderChanged)


class Reconstruction(UIGraph):
    """
    Specialization of a UIGraph designed to manage a 3D reconstruction.
    """

    imageExtensions = ('.jpg', '.jpeg', '.tif', '.tiff', '.png', '.exr')

    def __init__(self, graphFilepath='', parent=None):
        super(Reconstruction, self).__init__(graphFilepath, parent)
        self._buildIntrinsicsThread = None
        self._buildingIntrinsics = False
        self._cameraInit = None
        self._cameraInits = QObjectListModel(parent=self)
        self._endChunk = None
        self._meshFile = ''
        self.intrinsicsBuilt.connect(self.onIntrinsicsAvailable)
        self.graphChanged.connect(self.onGraphChanged)
        self._liveSfmManager = LiveSfmManager(self)

        # SfM result
        self._sfm = None
        self._views = None
        self._poses = None

        if graphFilepath:
            self.onGraphChanged()
        else:
            self.new()

    @Slot()
    def new(self):
        """ Create a new photogrammetry pipeline. """
        self.setGraph(multiview.photogrammetryPipeline())

    def onGraphChanged(self):
        """ React to the change of the internal graph. """
        self._liveSfmManager.reset()
        self._sfm = None
        self._endChunk = None
        self.setMeshFile('')
        self.updateCameraInits()
        if not self._graph:
            return

        self.setSfm(self.lastSfmNode())
        try:
            endNode = self._graph.findNode("Texturing")
            self._endChunk = endNode.getChunks()[0]  # type: graph.NodeChunk
            endNode.outputMesh.valueChanged.connect(self.updateMeshFile)
            self._endChunk.statusChanged.connect(self.updateMeshFile)
            self.updateMeshFile()
        except KeyError:
            self._endChunk = None
        # TODO: listen specifically for cameraInit creation/deletion
        self._graph.nodes.countChanged.connect(self.updateCameraInits)

    @staticmethod
    def runAsync(func, args=(), kwargs=None):
        thread = Thread(target=func, args=args, kwargs=kwargs)
        thread.start()
        return thread

    def getViewpoints(self):
        """ Return the Viewpoints model. """
        # TODO: handle multiple Viewpoints models
        return self._cameraInit.viewpoints.value if self._cameraInit else None

    def updateCameraInits(self):
        cameraInits = self._graph.nodesByType("CameraInit", sortedByIndex=True)
        if set(self._cameraInits.objectList()) == set(cameraInits):
            return
        self._cameraInits.setObjectList(cameraInits)
        self.setCameraInit(cameraInits[0] if cameraInits else None)

    def setCameraInit(self, cameraInit):
        """ Set the internal CameraInit node. """
        # TODO: handle multiple CameraInit nodes
        if self._cameraInit == cameraInit:
            return
        self._cameraInit = cameraInit
        self.cameraInitChanged.emit()

    def getCameraInitIndex(self):
        if not self._cameraInit:
            return -1
        return self._cameraInits.indexOf(self._cameraInit)

    def setCameraInitIndex(self, idx):
        self.setCameraInit(self._cameraInits[idx])

    def updateMeshFile(self):
        if self._endChunk and self._endChunk.status.status == graph.Status.SUCCESS:
            self.setMeshFile(self._endChunk.node.outputMesh.value)
        else:
            self.setMeshFile('')

    def setMeshFile(self, mf):
        if self._meshFile == mf:
            return
        self._meshFile = mf
        self.meshFileChanged.emit()

    def lastSfmNode(self):
        """ Retrieve the last SfM node from the initial CameraInit node. """
        sfmNodes = self._graph.nodesFromNode(self._cameraInits[0], 'StructureFromMotion')[0]
        return sfmNodes[-1] if sfmNodes else None

    def addSfmAugmentation(self):
        """
        Create a new augmentation step connected to the last SfM node of this Reconstruction and
        return the created CameraInit and SfM nodes.

        If the Reconstruction is not initialized (empty initial CameraInit), this method won't
        create anything and return initial CameraInit and SfM nodes.

        Returns:
            Node, Node: CameraInit, StructureFromMotion
        """
        sfm = self.lastSfmNode()
        if not sfm:
            return None, None

        if len(self._cameraInits) == 1:
            assert self._cameraInit == self._cameraInits[0]
            # Initial CameraInit is empty, use this one
            if len(self._cameraInits[0].viewpoints) == 0:
                return self._cameraInit, sfm

        with self.groupedGraphModification("SfM Augmentation"):
            # instantiate sfm augmentation chain
            cameraInit = self.addNode('CameraInit')
            featureExtraction = self.addNode('FeatureExtraction')
            imageMatching = self.addNode('ImageMatchingMultiSfM')
            featureMatching = self.addNode('FeatureMatching')
            structureFromMotion = self.addNode('StructureFromMotion')

            edges = (
                (cameraInit.output, featureExtraction.input),
                (featureExtraction.input, imageMatching.input),
                (featureExtraction.output, imageMatching.featuresFolder),
                (imageMatching.featuresFolder, featureMatching.featuresFolder),
                (imageMatching.outputCombinedSfM, featureMatching.input),
                (imageMatching.output, featureMatching.imagePairsList),
                (featureMatching.input, structureFromMotion.input),
                (featureMatching.featuresFolder, structureFromMotion.featuresFolder),
                (featureMatching.output, structureFromMotion.matchesFolder),
            )
            for src, dst in edges:
                self.addEdge(src, dst)

            # connect last SfM node to ImageMatchingMultiSfm
            self.addEdge(sfm.output, imageMatching.inputB)

        return cameraInit, structureFromMotion

    @Slot(QObject, graph.Node)
    def handleFilesDrop(self, drop, cameraInit):
        """ Handle drop events aiming to add images to the Reconstruction.
        Fetching urls from dropEvent is generally expensive in QML/JS (bug ?).
        This method allows to reduce process time by doing it on Python side.
        """
        self.importImages(self.getImageFilesFromDrop(drop), cameraInit)

    @staticmethod
    def isImageFile(filepath):
        """ Return whether filepath is a path to an image file supported by Meshroom. """
        return os.path.splitext(filepath)[1].lower() in Reconstruction.imageExtensions

    @staticmethod
    def getImageFilesFromDrop(drop):
        urls = drop.property("urls")
        # Build the list of images paths
        images = []
        for url in urls:
            localFile = url.toLocalFile()
            if os.path.isdir(localFile):  # get folder content
                files = [os.path.join(localFile, f) for f in os.listdir(localFile)]
            else:
                files = [localFile]
            images.extend([f for f in files if Reconstruction.isImageFile(f)])
        return images

    def importImages(self, images, cameraInit):
        """ Add the given list of images to the Reconstruction. """
        # Start the process of updating views and intrinsics
        self._buildIntrinsicsThread = self.runAsync(self.buildIntrinsics, args=(cameraInit, images,))

    def buildIntrinsics(self, cameraInit, additionalViews):
        """
        Build up-to-date intrinsics and views based on already loaded + additional images.
        Does not modify the graph, can be called outside the main thread.
        Emits intrinsicBuilt(views, intrinsics) when done.
        """
        try:
            self.setBuildingIntrinsics(True)
            # Retrieve the list of updated viewpoints and intrinsics
            views, intrinsics = cameraInit.nodeDesc.buildIntrinsics(cameraInit, additionalViews)
            self.intrinsicsBuilt.emit(cameraInit, views, intrinsics)
            return views, intrinsics
        except Exception:
            import traceback
            logging.error("Error while building intrinsics : {}".format(traceback.format_exc()))
        finally:
            self.setBuildingIntrinsics(False)

    def onIntrinsicsAvailable(self, cameraInit, views, intrinsics):
        """ Update CameraInit with given views and intrinsics. """
        with self.groupedGraphModification("Add Images"):
            self.setAttribute(cameraInit.viewpoints, views)
            self.setAttribute(cameraInit.intrinsics, intrinsics)
        self.setCameraInit(cameraInit)

    def setBuildingIntrinsics(self, value):
        if self._buildingIntrinsics == value:
            return
        self._buildingIntrinsics = value
        self.buildingIntrinsicsChanged.emit()

    cameraInitChanged = Signal()
    cameraInit = Property(QObject, lambda self: self._cameraInit, notify=cameraInitChanged)
    cameraInitIndex = Property(int, getCameraInitIndex, setCameraInitIndex, notify=cameraInitChanged)
    viewpoints = Property(QObject, getViewpoints, notify=cameraInitChanged)
    cameraInits = Property(QObject, lambda self: self._cameraInits, constant=True)
    intrinsicsBuilt = Signal(QObject, list, list)
    buildingIntrinsicsChanged = Signal()
    buildingIntrinsics = Property(bool, lambda self: self._buildingIntrinsics, notify=buildingIntrinsicsChanged)
    meshFileChanged = Signal()
    meshFile = Property(str, lambda self: self._meshFile, notify=meshFileChanged)
    liveSfmManager = Property(QObject, lambda self: self._liveSfmManager, constant=True)

    def updateViewsAndPoses(self):
        """
        Update internal views and poses based on the current SfM node.
        """
        assert self._sfm is not None
        self._views, self._poses = self._sfm.nodeDesc.getViewsAndPoses(self._sfm)
        self.sfmReportChanged.emit()

    def getSfm(self):
        """ Returns the current SfM node. """
        return self._sfm

    def setSfm(self, node):
        """ Set the current SfM node.
        This node will be used to retrieve sparse reconstruction result like camera poses.
        """
        if self._sfm:
            self._sfm.chunks[0].statusChanged.disconnect(self.updateViewsAndPoses)
        self._sfm = node
        # Update views and poses and do so each time
        # the status of the SfM node's only chunk changes
        self.updateViewsAndPoses()
        self._sfm.chunks[0].statusChanged.connect(self.updateViewsAndPoses)
        self.sfmChanged.emit()

    sfmChanged = Signal()
    sfm = Property(QObject, getSfm, setSfm, notify=sfmChanged)
    sfmReportChanged = Signal()
    views = Property("QVariant", lambda self: self._views, notify=sfmReportChanged)
    poses = Property("QVariant", lambda self: self._poses, notify=sfmReportChanged)
