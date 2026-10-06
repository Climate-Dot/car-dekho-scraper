-- Creates the CarDekho variant specs tables in a dedicated `cardekho` schema.
-- Additive only: creates the schema/tables if missing, never drops or alters existing objects.
-- Loaded by ingest_variant_specs.py (staging -> delete snapshot scope -> insert into fact).
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF SCHEMA_ID('cardekho') IS NULL
    EXEC('CREATE SCHEMA cardekho');

DECLARE @columns NVARCHAR(MAX) = N'
    [snapshot_date] DATE NOT NULL,
    [brand] NVARCHAR(100) NOT NULL,
    [model] NVARCHAR(200) NOT NULL,
    [variant] NVARCHAR(300) NOT NULL,
    [variant_label] NVARCHAR(400) NOT NULL,
    [is_top_selling] BIT NOT NULL,
    [fuel_type] NVARCHAR(20) NULL,
    [is_ev] BIT NOT NULL,
    [ex_showroom_price_inr] BIGINT NULL,
    [body_type] NVARCHAR(50) NULL,
    [body_type_raw] NVARCHAR(50) NULL,
    [seating_capacity] INT NULL,
    [boot_space_l] DECIMAL(7,1) NULL,
    [ground_clearance_unladen_mm] INT NULL,
    [length_mm] INT NULL,
    [width_mm] INT NULL,
    [height_mm] INT NULL,
    [max_power_bhp] DECIMAL(8,2) NULL,
    [max_power] NVARCHAR(100) NULL,
    [max_torque_nm] DECIMAL(8,2) NULL,
    [max_torque] NVARCHAR(100) NULL,
    [transmission_type] NVARCHAR(50) NULL,
    [fuel_tank_capacity_l] DECIMAL(6,1) NULL,
    [fuel_efficiency] DECIMAL(6,2) NULL,
    [fuel_efficiency_unit] NVARCHAR(10) NULL,
    [range_km] INT NULL,
    [battery_capacity_kwh] DECIMAL(7,2) NULL,
    [charging_port_type] NVARCHAR(100) NULL,
    [charging_options] NVARCHAR(500) NULL,
    [ac_charging_time] NVARCHAR(500) NULL,
    [dc_charging_time] NVARCHAR(500) NULL,
    [source_url] NVARCHAR(500) NULL,
    [source_file] NVARCHAR(200) NOT NULL,
    [specs_json] NVARCHAR(MAX) NOT NULL,
    [inserted_at] DATETIME2 NOT NULL';

IF OBJECT_ID('cardekho.staging_variant_specs', 'U') IS NULL
    EXEC(N'CREATE TABLE cardekho.staging_variant_specs (' + @columns + N')');

IF OBJECT_ID('cardekho.fact_variant_specs', 'U') IS NULL
BEGIN
    EXEC(N'CREATE TABLE cardekho.fact_variant_specs (' + @columns + N')');
    CREATE CLUSTERED INDEX cix_fact_variant_specs_snapshot_brand
        ON cardekho.fact_variant_specs (snapshot_date, brand);
    CREATE UNIQUE NONCLUSTERED INDEX uix_fact_variant_specs_variant
        ON cardekho.fact_variant_specs (snapshot_date, brand, model, variant_label);
END;
