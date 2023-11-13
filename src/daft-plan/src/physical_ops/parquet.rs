use std::sync::Arc;

use daft_core::schema::SchemaRef;
use daft_scan::Pushdowns;

use crate::{
    physical_plan::PhysicalPlan, sink_info::OutputFileInfo,
    source_info::LegacyExternalInfo as ExternalSourceInfo, PartitionSpec,
};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TabularScanParquet {
    pub projection_schema: SchemaRef,
    pub external_info: ExternalSourceInfo,
    pub partition_spec: Arc<PartitionSpec>,
    pub pushdowns: Pushdowns,
}

impl TabularScanParquet {
    pub(crate) fn new(
        projection_schema: SchemaRef,
        external_info: ExternalSourceInfo,
        partition_spec: Arc<PartitionSpec>,
        pushdowns: Pushdowns,
    ) -> Self {
        Self {
            projection_schema,
            external_info,
            partition_spec,
            pushdowns,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TabularWriteParquet {
    pub schema: SchemaRef,
    pub file_info: OutputFileInfo,
    // Upstream node.
    pub input: Arc<PhysicalPlan>,
}

impl TabularWriteParquet {
    pub(crate) fn new(
        schema: SchemaRef,
        file_info: OutputFileInfo,
        input: Arc<PhysicalPlan>,
    ) -> Self {
        Self {
            schema,
            file_info,
            input,
        }
    }
}
